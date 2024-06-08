import dataclasses
from collections import defaultdict
from typing import Optional, Dict, List, Union, Callable, Any
import torch

from tensor_beasts.util import (
    torch_correlate_2d, torch_correlate_3d, safe_sub, safe_add, timing,
    pad_matrix, get_direction_matrix, directional_kernel_set, generate_diffusion_kernel, generate_plant_crowding_kernel,
    safe_sum, perlin_noise
)


@dataclasses.dataclass
class Feature:
    group: Optional[str] = None
    tags: Optional[List[str]] = None
    tensor: Optional[torch.Tensor] = None


class FeatureGroup:
    pass


class BaseEntity:

    def __init__(self, entity_info: Dict[str, Any]):
        self.features = {}
        for feature_info in entity_info["features"]:
            self.features[feature_info["name"]] = Feature(
                group=feature_info.get("group"),
                tags=feature_info.get("tags", []),
            )

    def __getattr__(self, item):
        if item not in self.features:
            raise AttributeError(f"Feature {item} not found in entity.")
        return self.features[item].tensor

    def tagged(self, tag: str) -> List[torch.Tensor]:
        return [feature.tensor for feature in self.features.values() if tag in feature.tags]


class World:
    def __init__(self, size: int, config: Optional[Dict] = None, scalars: Optional[Dict] = None):
        """Initialize the world.

        The world is a tensor of unsigned 8-bit integers with shape (H, W, C) where the size of C is the total number of
        features defined in the config.

        On initialization of a World instance, the entity and feature structure defined in the config are mapped to
        class attributes that access the corresponding feature channels in the world tensor.

        For example, if the predator energy feature is the Nth feature channel, `world.predator.energy` is equivalent to
        `world_tensor[:, :, N]`.

        Assigning a feature to a group will ensure that the feature's index along the channel dimension is adjacent to
        other features in the same group. This is useful for operations that batch process features in the same group.
        A feature can only belong to one group.

        For example, if the features in the `energy` group are assigned to the nth to mth channels, `world.energy` is
        equivalent to `world_tensor[:, :, N:M]`.

        Assigning a feature with one or more tags will add that feature's name to the corresponding tag list.

        :param size: The value of the world's height and width. (H, W) == (size, size)
        :param config: The configuration of the world.
        :param scalars: The scalars of the world.
        """
        self.width, self.height = size, size

        # TODO: Use Hydra for config?
        self.config = {
            "entities": {
                "Predator": {
                    "features": [
                        {"name": "energy", "group": "energy"},
                        {"name": "scent", "group": "scent"},
                        {"name": "offspring_count", "tags": ["clear_on_death"]},
                        {"name": "id_0", "group": "predator_id", "tags": ["clear_on_death"]},
                        {"name": "id_1", "group": "predator_id", "tags": ["clear_on_death"]}
                    ]
                },
                "Plant": {
                    "features": [
                        {"name": "energy", "group": "energy"},
                        {"name": "scent", "group": "scent"},
                        {"name": "fertility_map"},
                        {"name": "seed"},
                        {"name": "crowding"}
                    ]
                },
                "Herbivore": {
                    "features": [
                        {"name": "energy", "group": "energy"},
                        {"name": "scent", "group": "scent"},
                        {"name": "offspring_count", "tags": ["clear_on_death"]},
                        {"name": "id_0", "group": "herbivore_id", "tags": ["clear_on_death"]},
                        {"name": "id_1", "group": "herbivore_id", "tags": ["clear_on_death"]},
                    ]
                },
                "Obstacle": {
                    "features": [
                        {"name": "mask"}
                    ]
                }
            }
        }
        if config is not None:
            self.config.update(config)

        self.scalars = {
            "plant_init_odds": 255,
            "plant_growth_step_modulo": 2,
            "herbivore_init_odds": 255,
            "plant_growth_odds": 255,
            "plant_germination_odds": 255,
            "plant_crowding_odds": 25,
            "plant_seed_odds": 255,
            "herbivore_eat_max": 16,
            "predator_eat_max": 255
        }
        if scalars is not None:
            self.scalars.update(scalars)

        self.entities = {}
        self.feature_groups = {}
        self.feature_tags = defaultdict(list)
        self.world_tensor = None

        self._initialize_entities()
        self._initialize_world_tensor()
        self._assign_features()

        for name, value in self.scalars.items():
            setattr(self, name, value)

        self.plant.energy[:] = (torch.randint(0, self.plant_init_odds, (self.width, self.height), dtype=torch.uint8) == 0)

        self.herbivore.energy[:] = (
            (torch.randint(0, self.herbivore_init_odds, (self.width, self.height), dtype=torch.uint8) == 0)
        ) * 240

        self.predator.energy[:] = (
            (torch.randint(0, self.herbivore_init_odds, (self.width, self.height), dtype=torch.uint8) == 0)
        ) * 240

        self.plant.fertility_map[:] = ((perlin_noise((size, size), (8, 8)) + 3) * 63).type(torch.uint8)

        # self.obstacle.mask[:] = (torch.randint(0, 256, (self.width, self.height), dtype=torch.uint8) == 0)
        # self.obstacle.mask[:] = generate_maze(size) * generate_maze(size)
        self.obstacle.mask[:] = torch.zeros((self.width, self.height), dtype=torch.uint8)

    @staticmethod
    def update_id(ids: torch.Tensor):
        """If ids[0] is 255, increment ids[1]."""
        ids[:, :, 1][ids[:, :, 0] == 255] += 1  # No-good extra assignment. Assigned in the caller too.
        return ids[:, :, 1]

    def _initialize_entities(self):
        """Initialize entities based on the configuration."""
        for entity_name, entity_info in self.config["entities"].items():
            entity_class = type(entity_name, (BaseEntity,), {})
            entity_instance = entity_class(entity_info)
            self.entities[entity_name.lower()] = entity_instance

    def _initialize_world_tensor(self):
        """Calculate the total depth and initialize the world tensor."""
        total_features = sum(
            len(entity_info["features"]) for entity_info in self.config["entities"].values()
        )
        self.world_tensor = torch.zeros((self.width, self.height, total_features), dtype=torch.uint8)

    def _assign_features(self):
        """Assign features and slices to entities and groups based on the config."""
        idx = 0
        group_feature_map = defaultdict(list)
        ungrouped_features = []

        # Collect features groups and tags
        for entity_name, entity_info in self.config["entities"].items():
            for feature in entity_info["features"]:
                if "group" in feature:
                    group_feature_map[feature["group"]].append(
                        (entity_name.lower(), feature)
                    )
                else:
                    ungrouped_features.append((entity_name.lower(), feature))

        # Start by assigning grouped features in contiguous slices
        for group, features in group_feature_map.items():
            group_start_idx = idx

            for entity_name, feature in features:
                entity = self.entities[entity_name]
                entity.features[feature["name"]].tensor = self.world_tensor[:, :, idx]
                idx += 1

                group_slice = self.world_tensor[:, :, group_start_idx:idx]
                setattr(self, group, group_slice)

        for entity_name, feature in ungrouped_features:
            entity = self.entities[entity_name]
            entity.features[feature["name"]].tensor = self.world_tensor[:, :, idx]
            idx += 1

    def __getattr__(self, name):
        """Provide convenient access to entities and feature groups."""
        if name in self.entities:
            return self.entities[name]
        elif name in self.feature_groups:
            return self.feature_groups[name]
        else:
            raise AttributeError(f"'World' object has no attribute '{name}'")

    def update(self, step):
        rand_array = torch.randint(0, 255, (self.width, self.height), dtype=torch.uint8)

        plant_mask = self.plant.energy.bool()

        if step % self.plant_growth_step_modulo == 0:
            self.plant.crowding = torch_correlate_2d(plant_mask, generate_plant_crowding_kernel(), mode='constant')
            self.grow(
                self.plant.energy,
                self.plant_growth_odds,
                self.plant.crowding,
                self.plant_crowding_odds,
                self.plant.fertility_map,
                rand_array
            )

            self.plant.seed |= self.plant.crowding > (rand_array % self.plant_seed_odds)

            self.germinate(self.plant.seed, self.plant.energy, self.plant_germination_odds, rand_array)

        self.move(
            entity_energy=self.herbivore.energy,
            target_energy=self.plant.scent,
            target_energy_weights=[0.5],
            opposite_energy=[self.herbivore.scent, self.predator.scent],
            opposite_energy_weights=[0.1, 1],
            carried_features_self=[self.herbivore.offspring_count, self.herbivore.id_0, self.herbivore.id_1],
            carried_feature_fns_self=[lambda x: safe_add(x, 1, inplace=False), lambda x: x, lambda x: x],
            carried_features_offspring=[self.herbivore.id_1, self.herbivore.id_0],
            carried_feature_fns_offspring=[lambda _: self.update_id(self.herbivore_id), lambda x: x + 1],
        )
        safe_sub(self.herbivore.energy, 2)

        self.move(
            entity_energy=self.predator.energy,
            target_energy=self.herbivore.scent,
            target_energy_weights=[1],
            opposite_energy=self.predator.scent,
            opposite_energy_weights=[1],
            carried_features_self=[self.predator.offspring_count, self.predator.id_0, self.predator.id_1],
            carried_feature_fns_self=[lambda x: safe_add(x, 1, inplace=False), lambda x: x, lambda x: x],
            carried_features_offspring=[self.herbivore.id_1, self.herbivore.id_0],
            carried_feature_fns_offspring=[lambda _: self.update_id(self.predator_id), lambda x: x + 1],

        )
        safe_sub(self.predator.energy, 1)

        self.eat(self.herbivore.energy, self.plant.energy, self.herbivore_eat_max)
        self.eat(self.predator.energy, self.herbivore.energy, self.predator_eat_max)

        self.diffuse_scent(self.energy, self.scent, mask=self.obstacle.mask)

        for entity in self.entities.values():
            for cleared_feature in entity.tagged('clear_on_death'):
                cleared_feature *= entity.energy > 0

        return {
            'seed_count': float(torch.sum(self.plant.seed)),
            'plant_mass': float(torch.sum(self.plant.energy)),
            'herbivore_mass': float(torch.sum(self.herbivore.energy)),
            'predator_mass': float(torch.sum(self.predator.energy)),
            'herbivore_offspring_count': float(torch.sum(self.herbivore.offspring_count)),
        }

    @staticmethod
    @timing
    def diffuse_scent(entity_energy, entity_scent, mask=None, diffusion_steps=1):
        for _ in range(diffusion_steps):
            scent = entity_scent.type(torch.float32) ** 2
            entity_scent[:] = torch.pow(torch_correlate_3d(scent, generate_diffusion_kernel().type(torch.float32)), 1.0 / 2.0).type(torch.uint8)
        safe_sub(entity_scent[:], 1)
        safe_add(entity_scent[:], entity_energy[:] // 4)
        if mask is not None:
            mask = torch.stack((mask, mask, mask), dim=-1) == 0
            entity_scent *= mask

    @staticmethod
    @timing
    def move(
        entity_energy: torch.Tensor,
        target_energy: Union[torch.Tensor, List[torch.Tensor]],
        target_energy_weights: List[float],
        opposite_energy: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
        opposite_energy_weights: Optional[List[float]] = None,
        clearance_mask: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
        clearance_kernel_size: Optional[int] = 5,
        divide_threshold: Optional[int] = 250,
        divide_fn_self: Optional[Callable] = lambda x: x // 2,
        divide_fn_offspring: Optional[Callable] = lambda x: x // 4,
        carried_features_self: Optional[List[torch.Tensor]] = None,
        carried_features_offspring: Optional[List[torch.Tensor]] = None,
        carried_feature_fns_self: Optional[List[Callable]] = None,
        carried_feature_fns_offspring: Optional[List[Callable]] = None,
    ):
        """
        Move entities and reproduce entities. Movement is based on the target energy tensor and optional opposite energy tensor.

        :param entity_energy: Energy tensor of the entity to move. This is transferred to the selected position.
            This is also used for clearance calculations, masking, and reproduction.
        :param target_energy: Movement will favor moving towards higher values in this tensor.
        :param target_energy_weights: Weights for the target energy tensor.
        :param opposite_energy: Movement will avoid moving towards higher values in this tensor.
        :param opposite_energy_weights: Weights for the opposite energy tensor.
        :param clearance_mask: Mask tensor for clearance calculations.
        :param clearance_kernel_size: Size of the kernel used for clearance calculations.
        :param divide_threshold: Energy threshold for reproduction.
        :param divide_fn_self: Function to apply to the entity energy on reproduction.
        :param divide_fn_offspring: Function to apply to the offspring energy on reproduction.
        :param carried_features_self: List of features that will be moved along with the entity energy.
        :param carried_features_offspring: List of features that will be copied from the parent to the offspring.
        :param carried_feature_fns_self: List of functions to apply to the carried features of the parent entity.
        :param carried_feature_fns_offspring: List of functions to apply to the carried features of the offspring entity.
        :return: None
        """
        if carried_features_self is not None:
            if carried_feature_fns_self is None:
                carried_feature_fns_self = [lambda x: x] * len(carried_features_self)
            else:
                assert len(carried_features_self) == len(carried_feature_fns_self)

        if carried_features_offspring is not None:
            if carried_feature_fns_offspring is None:
                carried_feature_fns_offspring = [lambda x: x] * len(carried_features_offspring)
            else:
                assert len(carried_features_offspring) == len(carried_feature_fns_offspring)

        if target_energy is not None:
            if not isinstance(target_energy, list):
                target_energy = [target_energy]

            if target_energy_weights is None:
                target_energy_weights = [1] * len(target_energy)
            else:
                assert len(target_energy) == len(target_energy_weights)

            target_energy = safe_sum([(target * wt).type(torch.uint8) for target, wt in zip(target_energy, target_energy_weights)])

        if opposite_energy is not None:
            if not isinstance(opposite_energy, list):
                opposite_energy = [opposite_energy]

            if opposite_energy_weights is None:
                opposite_energy_weights = [1] * len(opposite_energy)
            else:
                assert len(opposite_energy) == len(opposite_energy_weights)
            # Unsafe assumption that entity energy is always a component of the opposite energy!!
            # opposite_energy = (safe_sub(safe_sum(opposite_energy), entity_energy, inplace=False))
            opposite_energy = safe_sum([(opposite * wt).type(torch.uint8) for opposite, wt in zip(opposite_energy, opposite_energy_weights)])
            target_energy = safe_sub(target_energy, opposite_energy, inplace=False)

        if clearance_mask is not None:
            if isinstance(clearance_mask, list):
                clearance_mask = safe_sum(clearance_mask)
            else:
                clearance_mask = clearance_mask.clone()
            clearance_mask[entity_energy > 0] = 1
        else:
            clearance_mask = entity_energy > 0

        directions = get_direction_matrix(target_energy)

        # This is 1 where an entity is present and intends to move in that direction
        direction_masks = {d: ((directions == d) * (entity_energy > 0)).type(torch.uint8) for d in range(1, 5)}

        clearance_kernels = directional_kernel_set(clearance_kernel_size)
        # TODO: Batch this!
        for d in range(1, 5):
            direction_masks[d] *= ~(torch.tensor(
                torch_correlate_2d(
                    clearance_mask.type(torch.float32),
                    clearance_kernels[d].type(torch.float32),
                    mode='constant',
                    cval=1
                )
            ).type(torch.bool))

        # One where an entity is present in the current state and will move, and zero where it will not
        move_origin_mask = torch.sum(torch.stack(list(direction_masks.values())), dim=0).type(torch.bool)

        # One where an entity will move away and leave an offspring, and zero where it will not
        offspring_mask = move_origin_mask * (entity_energy > divide_threshold)

        # One where an entity will move away and leave no offspring, and zero where it will not
        vacated_mask = move_origin_mask * (entity_energy <= divide_threshold)

        # After this operation, each feature will be the union of its current state and the state after movement
        for feature, fn in [(entity_energy, divide_fn_self)] + list(zip(carried_features_self or [], carried_feature_fns_self or [])):
            safe_add(feature, torch.sum(torch.stack([
                # TODO: Batch? Which is faster, pad or roll?
                pad_matrix(
                    torch.where(
                        ((direction_masks[d] * entity_energy) > divide_threshold).type(torch.bool),
                        direction_masks[d] * fn(feature),
                        direction_masks[d] * feature
                    ),
                    d
                )
                for d in range(1, 5)
            ]), dim=0))

        # After this operation, each origin position where an offspring will be left will be adjusted by corresponding
        # feature functions
        for feature, fn in [(entity_energy, divide_fn_offspring)] + list(zip(carried_features_offspring or [], carried_feature_fns_offspring or [])):
            feature[:] = torch.where(
                offspring_mask,
                fn(feature),
                feature
            )

        # After this operation, each origin position where no offspring will be left will be zeroed
        for feature in [entity_energy] + (carried_features_self or []) + (carried_features_offspring or []):
            feature[:] *= ~vacated_mask

    @staticmethod
    @timing
    def eat(eater_energy, eaten_energy, eat_max, eat_efficiency_loss=4):
        old_eaten_energy = eaten_energy.clone()
        safe_sub(eaten_energy, (eater_energy > 0).type(torch.uint8) * eat_max)
        delta = old_eaten_energy - eaten_energy
        safe_add(eater_energy, delta // eat_efficiency_loss)

    @staticmethod
    @timing
    def germinate(seeds, plant_energy, germination_odds, rand_tensor):
        germination_rand = rand_tensor % germination_odds
        seed_germination = (
            seeds & ~(plant_energy > 0) & (germination_rand == 0)
        )
        safe_add(plant_energy, seed_germination)
        safe_sub(seeds, seed_germination)

    @staticmethod
    @timing
    def grow(plant_energy, plant_growth_odds, crowding, crowding_odds, fertility_map, rand_tensor):
        growth_rand = rand_tensor % plant_growth_odds
        growth = safe_add(plant_energy, fertility_map, inplace=False) <= growth_rand
        plant_crowding_mask = (rand_tensor % crowding_odds) >= crowding
        safe_add(plant_energy, (plant_energy > 0) * growth * plant_crowding_mask)
