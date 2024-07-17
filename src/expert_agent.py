##!/usr/bin/env python3
# @File: expert_agent.py
# @Description: An expert agent for data collection in CARLA Leaderboard 2.0.
# @CreatedTime: 2024/07/08
# @Author: Yueyuan Li, PDM-Lite


import logging
import math
from collections import deque

import carla
import numpy as np
from leaderboard.autoagents.autonomous_agent import AutonomousAgent
from scipy.integrate import RK45
from srunner.scenariomanager.carla_data_provider import CarlaDataProvider

from src.common_carla.actor import get_horizontal_distance
from src.common_carla.route import (
    get_previous_road_lane_ids,
    is_near_lane_change,
    is_overtaking_path_clear,
    sort_scenarios_by_distance,
)
from src.common_carla.traffic_light import get_before_traffic_light_waypoints
from src.controllers import LateralPIDController, LongitudinalLinearRegressionController
from src.leaderboard_custom.scenarios.cheater import Cheater
from src.planners.privileged_route_planner import PrivilegedRoutePlanner
from src.planners.route_planner import RoutePlanner
from src.utils.geometry import get_angle_by_position, normalize_angle
from src.utils.kinematic_bicycle_model import KinematicBicycleModel


def get_entry_point():
    return "ExpertAgent"


class ExpertAgent(AutonomousAgent):
    """An expert agent for data collection in CARLA Leaderboard 2.0. This agent has access to the ground truth in the simulator directly.

    Adopted from: https://github.com/autonomousvision/carla_garage/blob/leaderboard_2/team_code/autopilot.py
    """

    def setup(self, configs):
        self.configs = configs
        self.record = configs.record
        self.track = configs.track
        self.save_path = configs.save_path
        self.route_index = configs.route_index
        self.generate_data = configs.generate_data
        self.make_histogram = configs.make_histogram
        self.tp_stats = configs.tp_stats
        self.visualize = configs.visualize

        # Dynamics models
        self.ego_physics_model = KinematicBicycleModel(configs)
        self.vehicle_physics_model = KinematicBicycleModel(configs)

        self.world_map = CarlaDataProvider.get_map()
        self.ego_vehicle = CarlaDataProvider.get_hero_actor()
        self.world = CarlaDataProvider.get_world()

        # Check if the vehicle starts from a parking spot
        distance_to_road = self.global_plan_world_coord[0][0].location.distance(
            self.ego_vehicle.get_location()
        )

        # The first waypoint starts at the lane center, hence it will be more than 2 m away from the center of the ego vehicle at the beginning if the route starts with a parking exit scenario.
        starts_with_parking_exit = distance_to_road > 2

        # Setup planners
        self.waypoint_planner = PrivilegedRoutePlanner(configs.privileged_route_planner)
        self.waypoint_planner.setup_route(
            self.global_plan_world_coord,
            self.world,
            self.world_map,
            starts_with_parking_exit,
            self.ego_vehicle.get_location(),
        )
        self.waypoint_planner.save()

        self.command_planner = RoutePlanner(
            configs.route_planner.min_distance,
            configs.route_planner.max_distance,
        )
        self.command_planner.set_route(self._global_plan_world_coord)

        # Navigation command buffer, needed because the correct command comes from the last cleared waypoint
        self.commands = deque(maxlen=2)
        self.commands.append(4)
        self.commands.append(4)
        self.next_commands = deque(maxlen=2)
        self.next_commands.append(4)
        self.next_commands.append(4)
        self.target_point_prev = [1e5, 1e5, 1e5]

        # To avoid failing the ActorBlockedTest, the agent has to move at least 0.1 m/s every 179 ticks
        self.ego_blocked_for_ticks = 0

        # Setup controllers
        self.longitudinal_controller = LongitudinalLinearRegressionController(
            configs.longitudinal_controller
        )
        self.lateral_controller = LateralPIDController(configs.lateral_controller)

        # Initialize controls
        self.steer = 0.0
        self.throttle = 0.0
        self.brake = 0.0
        self.target_speed = self.configs.target_speed_fast

        self.augmentation_translation = 0
        self.augmentation_rotation = 0

        # Angle to the next waypoint, normalized in [-1, 1] corresponding to [-90, 90]
        self.angle = 0.0

        self._reset_flags()
        self.is_junction = False
        self.aim_wp = None  # Waypoint the expert is steering towards
        self.remaining_route = None  # Remaining route
        self.remaining_route_original = None  # Remaining original route
        self.cleared_stop_sign = False
        self.visible_walker_ids = []
        self.walker_past_pos = {}  # Position of walker in the last frame

        self.vehicle_lights = (
            carla.VehicleLightState.Position | carla.VehicleLightState.LowBeam
        )
        # Set up logging

        # Preprocess traffic lights
        all_actors = self.world.get_actors()
        for actor in all_actors:
            if "traffic_light" in actor.type_id:
                center, waypoints = get_before_traffic_light_waypoints(
                    actor, self.world_map
                )
                self.traffic_light_list.append((actor, center, waypoints))

        # Remove bugged 2-wheelers
        # https://github.com/carla-simulator/carla/issues/3670
        for actor in all_actors:
            if "vehicle" in actor.type_id:
                extent = actor.bounding_box.extent
                if extent.x < 0.001 or extent.y < 0.001 or extent.z < 0.001:
                    actor.destroy()

    def sensors(self):
        return

    def get_ego_state(self, input_data):
        # Get the vehicle's speed from its velocity vector
        speed = self.ego_vehicle.get_velocity().length()

        # Checks the compass for Nans and rotates it into the default CARLA coordinate system with range [-pi,pi].
        compass = input_data["imu"][1][-1]
        if math.isnan(compass):
            compass = 0.0
        # The minus 90.0 degree is because the compass sensor uses a different
        # coordinate system then CARLA. Check the coordinate_sytems.txt file
        compass = normalize_angle(compass - np.deg2rad(90.0))

        # Get the vehicle's position from its location
        position = self.ego_vehicle.get_location()
        gps = np.array([position.x, position.y, position.z])

        return gps, speed, compass

    def run_step(self, input_data):
        return

    def set_global_plan(self, global_plan_gps, global_plan_world_coord):
        """
        Set the plan (route) for the agent
        """
        super().set_global_plan(global_plan_gps, global_plan_world_coord)
        self.global_plan_gps = global_plan_gps
        self.global_plan_world_coord = global_plan_world_coord

    def _reset_flags(self):
        self.traffic_light_hazard = False
        self.stop_sign_hazard = False
        self.stop_sign_close = False
        self.vehicle_hazard = False
        self.vehicle_affecting_id = None
        self.walker_hazard = False
        self.walker_close = False

    def _manage_route_obstacle_scenarios(
        self, ego_speed, target_speed, route_waypoints, vehicle_list, route_points
    ):
        """This method handles various obstacle and scenario situations that may arise during navigation.

        It adjusts the target speed, modifies the route, and determines if the ego vehicle should keep driving or wait.

        The method supports different scenario types including:
        - InvadingTurn
        - Accident
        - ConstructionObstacle
        - ParkedObstacle
        - AccidentTwoWays
        - ConstructionObstacleTwoWays
        - ParkedObstacleTwoWays
        - VehicleOpensDoorTwoWays
        - HazardAtSideLaneTwoWays
        - HazardAtSideLane
        - YieldToEmergencyVehicle.

        Args:
            target_speed (float): The current target speed of the ego vehicle.
            route_waypoints (list): A list of waypoints representing the current route.
            vehicle_list (list): A list of all vehicles in the simulation.
            route_points (numpy.ndarray): A numpy array containing the current route points.

        Returns:
            tuple: A tuple containing the updated target speed, a boolean indicating whether to keep driving,
                and a list containing information about a potential decreased target speed due to an object.
        """
        keep_driving = False
        speed_reduced_by_obj = [
            target_speed,
            None,
            None,
            None,
        ]  # [target_speed, type, id, distance]

        # Remove scenarios that ended with a scenario timeout
        active_scenarios = Cheater.active_scenarios.copy()
        for i, (scenario_type, scenario_data) in enumerate(active_scenarios):
            first_actor, last_actor = scenario_data[:2]
            if not first_actor.is_alive or (
                last_actor is not None and not last_actor.is_alive
            ):
                Cheater.active_scenarios.remove(active_scenarios[i])

        # Only continue if there are some active scenarios available
        if len(Cheater.active_scenarios) != 0:
            ego_location = self.ego_vehicle.get_location()

            # Sort the scenarios by distance if there is more than one active scenario
            if len(Cheater.active_scenarios) != 1:
                sort_scenarios_by_distance(ego_location)

            scenario_type, scenario_data = Cheater.active_scenarios[0]

            if scenario_type == "InvadingTurn":
                first_cone, last_cone, offset = scenario_data

                closest_distance = first_cone.get_location().distance(ego_location)

                if (
                    closest_distance
                    < self.configs.default_max_distance_to_process_scenario
                ):
                    self.waypoint_planner.shift_route_for_invading_turn(
                        first_cone, last_cone, offset
                    )
                    Cheater.active_scenarios = Cheater.active_scenarios[1:]

            elif scenario_type in [
                "Accident",
                "ConstructionObstacle",
                "ParkedObstacle",
            ]:
                first_actor, last_actor, direction = scenario_data[:3]

                horizontal_distance = get_horizontal_distance(
                    self.ego_vehicle, first_actor
                )

                # Shift the route around the obstacles
                if (
                    horizontal_distance
                    < self.configs.default_max_distance_to_process_scenario
                ):
                    transition_length = {
                        "Accident": self.configs.transition_smoothness_distance,
                        "ConstructionObstacle": self.configs.transition_smoothness_factor_construction_obstacle,
                        "ParkedObstacle": self.configs.transition_smoothness_distance,
                    }[scenario_type]
                    _, _ = self.waypoint_planner.shift_route_around_actors(
                        first_actor, last_actor, direction, transition_length
                    )
                    Cheater.active_scenarios = Cheater.active_scenarios[1:]

            elif scenario_type in [
                "AccidentTwoWays",
                "ConstructionObstacleTwoWays",
                "ParkedObstacleTwoWays",
                "VehicleOpensDoorTwoWays",
            ]:
                (
                    first_actor,
                    last_actor,
                    direction,
                    changed_route,
                    from_index,
                    to_index,
                    path_clear,
                ) = scenario_data

                # change the route if the ego is close enough to the obstacle
                horizontal_distance = get_horizontal_distance(
                    self.ego_vehicle, first_actor
                )

                # Shift the route around the obstacles
                if (
                    horizontal_distance
                    < self.configs.default_max_distance_to_process_scenario
                    and not changed_route
                ):
                    transition_length = {
                        "AccidentTwoWays": self.configs.transition_length_accident_two_ways,
                        "ConstructionObstacleTwoWays": self.configs.transition_length_construction_obstacle_two_ways,
                        "ParkedObstacleTwoWays": self.configs.transition_length_parked_obstacle_two_ways,
                        "VehicleOpensDoorTwoWays": self.configs.transition_length_vehicle_opens_door_two_ways,
                    }[scenario_type]
                    add_before_length = {
                        "AccidentTwoWays": self.configs.add_before_accident_two_ways,
                        "ConstructionObstacleTwoWays": self.configs.add_before_construction_obstacle_two_ways,
                        "ParkedObstacleTwoWays": self.configs.add_before_parked_obstacle_two_ways,
                        "VehicleOpensDoorTwoWays": self.configs.add_before_vehicle_opens_door_two_ways,
                    }[scenario_type]
                    add_after_length = {
                        "AccidentTwoWays": self.configs.add_after_accident_two_ways,
                        "ConstructionObstacleTwoWays": self.configs.add_after_construction_obstacle_two_ways,
                        "ParkedObstacleTwoWays": self.configs.add_after_parked_obstacle_two_ways,
                        "VehicleOpensDoorTwoWays": self.configs.add_after_vehicle_opens_door_two_ways,
                    }[scenario_type]
                    factor = {
                        "AccidentTwoWays": self.configs.factor_accident_two_ways,
                        "ConstructionObstacleTwoWays": self.configs.factor_construction_obstacle_two_ways,
                        "ParkedObstacleTwoWays": self.configs.factor_parked_obstacle_two_ways,
                        "VehicleOpensDoorTwoWays": self.configs.factor_vehicle_opens_door_two_ways,
                    }[scenario_type]

                    (
                        from_index,
                        to_index,
                    ) = self.waypoint_planner.shift_route_around_actors(
                        first_actor,
                        last_actor,
                        direction,
                        transition_length,
                        factor,
                        add_before_length,
                        add_after_length,
                    )

                    changed_route = True
                    scenario_data[3] = changed_route
                    scenario_data[4] = from_index
                    scenario_data[5] = to_index

                # Check if the ego can overtake the obstacle
                if (
                    changed_route
                    and from_index - self.waypoint_planner.route_index
                    < self.configs.max_distance_to_overtake_two_way_scnearios
                    and not path_clear
                ):
                    # Get previous roads and lanes of the target lane
                    target_lane = (
                        route_waypoints[0].get_left_lane()
                        if direction == "right"
                        else route_waypoints[0].get_right_lane()
                    )
                    if target_lane is None:
                        return target_speed, keep_driving, speed_reduced_by_obj
                    prev_road_lane_ids = get_previous_road_lane_ids(
                        target_lane, self.configs.previous_road_lane_retrieve_distance
                    )

                    overtake_speed = (
                        self.configs.overtake_speed_vehicle_opens_door_two_ways
                        if scenario_type == "VehicleOpensDoorTwoWays"
                        else self.configs.default_overtake_speed
                    )
                    path_clear = is_overtaking_path_clear(
                        self.world_map,
                        self.ego_vehicle,
                        vehicle_list,
                        target_speed,
                        self.waypoint_planner.route_points,
                        from_index,
                        to_index,
                        prev_road_lane_ids,
                        self.configs.check_path_free_safety_distance,
                        self.configs.check_path_free_safety_time,
                        min_speed=overtake_speed,
                    )

                    scenario_data[6] = path_clear

                # If the overtaking path is clear, keep driving; otherwise, wait behind the obstacle
                if path_clear:
                    if (
                        self.waypoint_planner.route_index
                        >= to_index
                        - self.configs.distance_to_delete_scenario_in_two_ways
                    ):
                        Cheater.active_scenarios = Cheater.active_scenarios[1:]
                    target_speed = {
                        "AccidentTwoWays": self.configs.default_overtake_speed,
                        "ConstructionObstacleTwoWays": self.configs.default_overtake_speed,
                        "ParkedObstacleTwoWays": self.configs.default_overtake_speed,
                        "VehicleOpensDoorTwoWays": self.configs.overtake_speed_vehicle_opens_door_two_ways,
                    }[scenario_type]
                    keep_driving = True
                else:
                    distance_to_leading_actor = (
                        float(from_index + 15 - self.waypoint_planner.route_index)
                        / self.configs.points_per_meter
                    )
                    target_speed = self._get_speed_idm(
                        ego_speed,
                        target_speed,
                        self.ego_vehicle.bounding_box.extent.x,
                        0,
                        distance_to_leading_actor,
                        s0=self.configs.idm.two_way_scenarios_minimum_distance,
                        T=self.configs.idm.two_way_scenarios_time_headway,
                    )

                    # Update the object causing the most speed reduction
                    if (
                        speed_reduced_by_obj is None
                        or speed_reduced_by_obj[0] > target_speed
                    ):
                        speed_reduced_by_obj = [
                            target_speed,
                            first_actor.type_id,
                            first_actor.id,
                            distance_to_leading_actor,
                        ]

            elif scenario_type == "HazardAtSideLaneTwoWays":
                (
                    first_actor,
                    last_actor,
                    changed_route,
                    from_index,
                    to_index,
                    path_clear,
                ) = scenario_data

                horizontal_distance = get_horizontal_distance(
                    self.ego_vehicle, first_actor
                )

                if (
                    horizontal_distance
                    < self.configs.max_distance_to_process_hazard_at_side_lane_two_ways
                    and not changed_route
                ):
                    to_index = self.waypoint_planner.get_closest_route_index(
                        self.waypoint_planner.route_index, last_actor.get_location()
                    )

                    # Assume the bicycles don't drive more than 7.5 m during the overtaking process
                    to_index += 135
                    from_index = self.waypoint_planner.route_index

                    starting_wp = route_waypoints[0].get_left_lane()
                    prev_road_lane_ids = get_previous_road_lane_ids(starting_wp)
                    path_clear = is_overtaking_path_clear(
                        self.world_map,
                        self.ego_vehicle,
                        vehicle_list,
                        target_speed,
                        self.waypoint_planner.route_points,
                        from_index,
                        to_index,
                        prev_road_lane_ids,
                        self.configs.check_path_free_safety_distance,
                        self.configs.check_path_free_safety_time,
                        min_speed=self.configs.default_overtake_speed,
                    )

                    if path_clear:
                        transition_length = self.configs.transition_smoothness_distance
                        self.waypoint_planner.shift_route_smoothly(
                            from_index, to_index, True, transition_length
                        )
                        changed_route = True
                        scenario_data[2] = changed_route
                        scenario_data[3] = from_index
                        scenario_data[4] = to_index
                        scenario_data[5] = path_clear

                # the overtaking path is clear
                if path_clear:
                    # Check if the overtaking is done
                    if self.waypoint_planner.route_index >= to_index:
                        Cheater.active_scenarios = Cheater.active_scenarios[1:]
                    # Overtake with max. 50 km/h
                    target_speed, keep_driving = (
                        self.configs.default_overtake_speed,
                        True,
                    )

            elif scenario_type == "HazardAtSideLane":
                (
                    first_actor,
                    last_actor,
                    changed_first_part_of_route,
                    from_index,
                    to_index,
                    path_clear,
                ) = scenario_data

                horizontal_distance = get_horizontal_distance(
                    self.ego_vehicle, last_actor
                )

                if (
                    horizontal_distance
                    < self.configs.max_distance_to_process_hazard_at_side_lane
                    and not changed_first_part_of_route
                ):
                    transition_length = self.configs.transition_smoothness_distance
                    (
                        from_index,
                        to_index,
                    ) = self.waypoint_planner.shift_route_around_actors(
                        first_actor, last_actor, "right", transition_length
                    )

                    to_index -= transition_length
                    changed_first_part_of_route = True
                    scenario_data[2] = changed_first_part_of_route
                    scenario_data[3] = from_index
                    scenario_data[4] = to_index

                if changed_first_part_of_route:
                    to_idx_ = self.waypoint_planner.extend_lane_shift_transition_for_hazard_at_side_lane(
                        last_actor, to_index
                    )
                    to_index = to_idx_
                    scenario_data[4] = to_index

                if self.waypoint_planner.route_index > to_index:
                    Cheater.active_scenarios = Cheater.active_scenarios[1:]

            elif scenario_type == "YieldToEmergencyVehicle":
                (
                    emergency_veh,
                    _,
                    changed_route,
                    from_index,
                    to_index,
                    to_left,
                ) = scenario_data

                horizontal_distance = get_horizontal_distance(
                    self.ego_vehicle, emergency_veh
                )

                if (
                    horizontal_distance
                    < self.configs.default_max_distance_to_process_scenario
                    and not changed_route
                ):
                    # Assume the emergency vehicle doesn't drive more than 20 m during the overtaking process
                    from_index = (
                        self.waypoint_planner.route_index
                        + 30 * self.configs.points_per_meter
                    )
                    to_index = (
                        from_index
                        + int(2 * self.configs.points_per_meter)
                        * self.configs.points_per_meter
                    )

                    transition_length = self.configs.transition_smoothness_distance
                    to_left = (
                        self.waypoint_planner.route_waypoints[from_index].lane_change
                        != carla.LaneChange.Right
                    )
                    self.waypoint_planner.shift_route_smoothly(
                        from_index, to_index, to_left, transition_length
                    )

                    changed_route = True
                    to_index -= transition_length
                    scenario_data[2] = changed_route
                    scenario_data[3] = from_index
                    scenario_data[4] = to_index
                    scenario_data[5] = to_left

                if changed_route:
                    to_idx_ = self.waypoint_planner.extend_lane_shift_transition_for_yield_to_emergency_vehicle(
                        to_left, to_index
                    )
                    to_index = to_idx_
                    scenario_data[4] = to_index

                    # Check if the emergency vehicle is in front of the ego vehicle
                    diff = emergency_veh.get_location() - ego_location
                    dot_res = (
                        self.ego_vehicle.get_transform().get_forward_vector().dot(diff)
                    )
                    if dot_res > 0:
                        Cheater.active_scenarios = Cheater.active_scenarios[1:]

        return target_speed, keep_driving, speed_reduced_by_obj

    def _get_speed_idm(
        self,
        ego_speed,
        target_speed,
        leading_actor_length,
        leading_actor_speed,
        distance_to_leading_actor,
        s0=4.0,
        T=0.5,
    ):
        """Compute the target speed for the ego vehicle using the Intelligent Driver Model (IDM).

        Args:
            s0 (float, optional): The minimum desired net distance.
            T (float, optional): The desired time headway.

        Returns:
            float: The target speed for the ego vehicle.
        """
        a = self.configs.idm.maximum_acceleration  # Maximum acceleration [m/s²]
        b = (
            self.configs.idm.comfortable_braking_deceleration_high_speed
            if ego_speed > self.configs.idm.comfortable_braking_deceleration_threshold
            else self.configs.idm.comfortable_braking_deceleration_low_speed
        )  # Comfortable deceleration [m/s²]
        delta = self.configs.idm.acceleration_exponent  # Acceleration exponent

        t_bound = self.configs.idm.t_bound

        def idm_equations(t, x):
            """
            Differential equations for the Intelligent Driver Model.

            Args:
                t (float): Time.
                x (list): State variables [position, speed].

            Returns:
                list: Derivatives of the state variables.
            """
            ego_position, ego_speed = x

            speed_diff = ego_speed - leading_actor_speed
            s_star = s0 + ego_speed * T + ego_speed * speed_diff / 2.0 / np.sqrt(a * b)
            # The maximum is needed to avoid numerical un-stability
            s = max(
                0.1,
                distance_to_leading_actor
                + t * leading_actor_speed
                - ego_position
                - leading_actor_length,
            )
            dvdt = a * (1.0 - (ego_speed / target_speed) ** delta - (s_star / s) ** 2)

            return [ego_speed, dvdt]

        # Set the initial conditions
        y0 = [0.0, ego_speed]

        # Integrate the differential equations using RK45
        rk45 = RK45(fun=idm_equations, t0=0.0, y0=y0, t_bound=t_bound)
        while rk45.status == "running":
            rk45.step()

        # The target speed is the final speed obtained from the integration
        target_speed = rk45.y[1]

        # Clip the target speed to non-negative values
        return np.clip(target_speed, 0, np.inf)

    def _get_speed_wrt_leading_vehicle(
        self,
        initial_target_speed,
        predicted_bounding_boxes,
        near_lane_change,
        leading_vehicle_ids,
        rear_vehicle_ids,
        speed_reduced_by_obj,
        plant,
    ):
        """Compute the target speed for the ego vehicle considering the leading vehicle.

        Args:
            initial_target_speed (float): The initial target speed for the ego vehicle.
            predicted_bounding_boxes (dict): A dictionary mapping actor IDs to lists of predicted bounding boxes.
            near_lane_change (bool): Whether the ego vehicle is near a lane change maneuver.
            leading_vehicle_ids (list): A list of IDs for vehicles in front of the ego vehicle.
            rear_vehicle_ids (list): A list of IDs for vehicles behind the ego vehicle.
            speed_reduced_by_obj (list or None): A list containing [reduced speed, object type, object ID, distance] for the object that caused the most speed reduction, or None if no speed reduction.
            plant (bool): Whether to use plant.

        Returns:
            float: The target speed for the ego vehicle considering the leading vehicle.
        """
        target_speed_wrt_leading_vehicle = initial_target_speed
        ego_location = self.ego_vehicle.get_location()

        if not plant:
            for vehicle_id, _ in predicted_bounding_boxes.items():
                if vehicle_id in leading_vehicle_ids and not near_lane_change:
                    # Vehicle is in front of the ego vehicle
                    ego_speed = self.ego_vehicle.get_velocity().length()
                    vehicle = self.world.get_actor(vehicle_id)
                    other_speed = vehicle.get_velocity().length()
                    distance_to_vehicle = ego_location.distance(vehicle.get_location())

                    # Compute the target speed using the IDM
                    target_speed_wrt_leading_vehicle = min(
                        target_speed_wrt_leading_vehicle,
                        self._get_speed_idm(
                            ego_speed,
                            initial_target_speed,
                            vehicle.bounding_box.extent.x * 2,
                            other_speed,
                            distance_to_vehicle,
                            s0=self.configs.idm.leading_vehicle_minimum_distance,
                            T=self.configs.idm.leading_vehicle_time_headway,
                        ),
                    )

                    # Update the object causing the most speed reduction
                    if (
                        speed_reduced_by_obj is None
                        or speed_reduced_by_obj[0] > target_speed_wrt_leading_vehicle
                    ):
                        speed_reduced_by_obj = [
                            target_speed_wrt_leading_vehicle,
                            vehicle.type_id,
                            vehicle.id,
                            distance_to_vehicle,
                        ]

            if self.visualize:
                for vehicle_id in predicted_bounding_boxes.keys():
                    # check if vehicle is in front of the ego vehicle
                    if (
                        vehicle_id in leading_vehicle_ids and not near_lane_change
                    ) or vehicle_id in rear_vehicle_ids:
                        vehicle = self.world.get_actor(vehicle_id)
                        extent = vehicle.bounding_box.extent
                        bb = carla.BoundingBox(vehicle.get_location(), extent)
                        bb.rotation = carla.Rotation(
                            pitch=0, yaw=vehicle.get_transform().rotation.yaw, roll=0
                        )
                        self.world.debug.draw_box(
                            box=bb,
                            rotation=bb.rotation,
                            thickness=0.5,
                            color=self.configs.leading_vehicle_color,
                            life_time=self.configs.draw_life_time,
                        )

        return target_speed_wrt_leading_vehicle, speed_reduced_by_obj

    def _get_speed_wrt_all_actors(
        self,
        initial_target_speed,
        ego_bounding_boxes,
        predicted_bounding_boxes,
        near_lane_change,
        leading_vehicle_ids,
        rear_vehicle_ids,
        speed_reduced_by_obj,
        nearby_walkers,
        nearby_walkers_ids,
    ):
        """Compute the target speeds for the ego vehicle considering all actors (vehicles, bicycles,
        and walkers) by checking for intersecting bounding boxes.

        Args:
            initial_target_speed (float): The initial target speed for the ego vehicle.
            ego_bounding_boxes (list): A list of bounding boxes for the ego vehicle at different future frames.
            predicted_bounding_boxes (dict): A dictionary mapping actor IDs to lists of predicted bounding boxes.
            near_lane_change (bool): Whether the ego vehicle is near a lane change maneuver.
            leading_vehicle_ids (list): A list of IDs for vehicles in front of the ego vehicle.
            rear_vehicle_ids (list): A list of IDs for vehicles behind the ego vehicle.
            speed_reduced_by_obj (list or None): A list containing [reduced speed, object type, object ID, distance] for the object that caused the most speed reduction, or None if no speed reduction.
            nearby_walkers (dict): A list of predicted bounding boxes of nearby walkers.
            nearby_walkers_ids (list): A list of IDs for nearby walkers.
        """
        target_speed_bicycle = initial_target_speed
        target_speed_walker = initial_target_speed
        target_speed_vehicle = initial_target_speed
        ego_location = self.ego_vehicle.get_location()
        hazard_color = self.configs.ego_vehicle.forecasted_bbs_hazard_color
        normal_color = self.configs.ego_vehicle.forecasted_bbs_normal_color
        color = normal_color

        # Iterate over the ego vehicle's bounding boxes and predicted bounding boxes of other actors
        for i, ego_bounding_box in enumerate(ego_bounding_boxes):
            for vehicle_id, bounding_boxes in predicted_bounding_boxes.items():
                # Skip leading and rear vehicles if not near a lane change
                if vehicle_id in leading_vehicle_ids and not near_lane_change:
                    continue
                elif vehicle_id in rear_vehicle_ids and not near_lane_change:
                    continue
                else:
                    # Check if the ego bounding box intersects with the predicted bounding box of the actor
                    intersects_with_ego = self.check_obb_intersection(
                        ego_bounding_box, bounding_boxes[i]
                    )
                    ego_speed = self.ego_vehicle.get_velocity().length()

                    if intersects_with_ego:
                        blocking_actor = self.world.get_actor(vehicle_id)

                        # Handle the case when the blocking actor is a bicycle
                        if (
                            "base_type" in blocking_actor.attributes
                            and blocking_actor.attributes["base_type"] == "bicycle"
                        ):
                            other_speed = blocking_actor.get_velocity().length()
                            distance_to_actor = ego_location.distance(
                                blocking_actor.get_location()
                            )

                            # Compute the target speed for bicycles using the IDM
                            target_speed_bicycle = min(
                                target_speed_bicycle,
                                self._get_speed_idm(
                                    ego_speed,
                                    initial_target_speed,
                                    blocking_actor.bounding_box.extent.x * 2,
                                    other_speed,
                                    distance_to_actor,
                                    s0=self.configs.idm.bicycle_minimum_distance,
                                    T=self.configs.idm.bicycle_desired_time_headway,
                                ),
                            )

                            # Update the object causing the most speed reduction
                            if (
                                speed_reduced_by_obj is None
                                or speed_reduced_by_obj[0] > target_speed_bicycle
                            ):
                                speed_reduced_by_obj = [
                                    target_speed_bicycle,
                                    blocking_actor.type_id,
                                    blocking_actor.id,
                                    distance_to_actor,
                                ]

                        # Handle the case when the blocking actor is not a bicycle
                        else:
                            self.vehicle_hazard = True  # Set the vehicle hazard flag
                            self.vehicle_affecting_id = vehicle_id  # Store the ID of the vehicle causing the hazard
                            color = hazard_color  # Change the following colors from green to red (no hazard to hazard)
                            target_speed_vehicle = 0
                            distance_to_actor = blocking_actor.get_location().distance(
                                ego_location
                            )

                            # Update the object causing the most speed reduction
                            if (
                                speed_reduced_by_obj is None
                                or speed_reduced_by_obj[0] > target_speed_vehicle
                            ):
                                speed_reduced_by_obj = [
                                    target_speed_vehicle,
                                    blocking_actor.type_id,
                                    blocking_actor.id,
                                    distance_to_actor,
                                ]

            # Iterate over nearby walkers and check for intersections with the ego bounding box
            for walker_bb, walker_id in zip(nearby_walkers, nearby_walkers_ids):
                if self.check_obb_intersection(ego_bounding_box, walker_bb[i]):
                    color = hazard_color
                    ego_speed = self.ego_vehicle.get_velocity().length()
                    blocking_actor = self.world.get_actor(walker_id)
                    distance_to_actor = ego_location.distance(
                        blocking_actor.get_location()
                    )

                    # Compute the target speed for walkers using the IDM
                    target_speed_walker = min(
                        target_speed_walker,
                        self._get_speed_idm(
                            ego_speed,
                            initial_target_speed,
                            0.5 + self.ego_vehicle.bounding_box.extent.x,
                            0.0,
                            distance_to_actor,
                            s0=self.configs.idm.walker_minimum_distance,
                            T=self.configs.idm.walker_desired_time_headway,
                        ),
                    )

                    # Update the object causing the most speed reduction
                    if (
                        speed_reduced_by_obj is None
                        or speed_reduced_by_obj[0] > target_speed_walker
                    ):
                        speed_reduced_by_obj = [
                            target_speed_walker,
                            blocking_actor.type_id,
                            blocking_actor.id,
                            distance_to_actor,
                        ]

            if self.visualize:
                self.world.debug.draw_box(
                    box=ego_bounding_box,
                    rotation=ego_bounding_box.rotation,
                    thickness=0.1,
                    color=color,
                    life_time=self.configs.draw_life_time,
                )

        return (
            target_speed_bicycle,
            target_speed_walker,
            target_speed_vehicle,
            speed_reduced_by_obj,
        )

    def _get_speed_affected_by_traffic_light(
        self,
        target_speed,
        distance_to_traffic_light,
        next_traffic_light,
    ):
        """Handles the behavior of the ego vehicle when approaching a traffic light.

        Args:
            target_speed (float): The current target speed of the ego vehicle.
            distance_to_traffic_light (float): The distance from the ego vehicle to the next traffic light.
            next_traffic_light (carla.TrafficLight or None): The next traffic light in the route.

        Returns:
            float: The adjusted target speed for the ego vehicle.
        """
        close_traffic_lights = []
        ego_location = self.ego_vehicle.get_location()
        ego_speed = self.ego_vehicle.get_velocity().length()

        for light, center, waypoints in self.traffic_light_list:
            center_loc = carla.Location(center)
            if center_loc.distance(ego_location) > self.configs.light_radius:
                continue

            for wp in waypoints:
                # * 0.9 to make the box slightly smaller than the street to prevent overlapping boxes.
                length_bounding_box = carla.Vector3D(
                    (wp.lane_width / 2.0) * 0.9,
                    light.trigger_volume.extent.y,
                    light.trigger_volume.extent.z,
                )
                length_bounding_box = carla.Vector3D(1.5, 1.5, 0.5)

                bounding_box = carla.BoundingBox(
                    wp.transform.location, length_bounding_box
                )

                global_rot = light.get_transform().rotation
                bounding_box.rotation = carla.Rotation(
                    pitch=global_rot.pitch, yaw=global_rot.yaw, roll=global_rot.roll
                )

                affects_ego = (
                    next_traffic_light is not None and light.id == next_traffic_light.id
                )

                close_traffic_lights.append(
                    [bounding_box, light.id, affects_ego, light.state]
                )

                if self.visualize:
                    if light.state == carla.libcarla.TrafficLightState.Red:
                        color = carla.Color(255, 0, 0, 255)
                    elif light.state == carla.libcarla.TrafficLightState.Yellow:
                        color = carla.Color(255, 255, 0, 255)
                    elif light.state == carla.libcarla.TrafficLightState.Green:
                        color = carla.Color(0, 255, 0, 255)
                    elif light.state == carla.libcarla.TrafficLightState.Off:
                        color = carla.Color(0, 0, 0, 255)
                    else:  # unknown
                        color = carla.Color(0, 0, 255, 255)

                    self.world.debug.draw_box(
                        box=bounding_box,
                        rotation=bounding_box.rotation,
                        thickness=0.1,
                        color=color,
                        life_time=0.051,
                    )

                    self.world.debug.draw_point(
                        wp.transform.location
                        + carla.Location(z=light.trigger_volume.location.z),
                        size=0.1,
                        color=color,
                        life_time=(1.0 / self.configs.carla_fps) + 1e-6,
                    )

        if (
            next_traffic_light is None
            or next_traffic_light.state == carla.TrafficLightState.Green
        ):
            # No traffic light or green light, continue with the current target speed
            return target_speed

        # Compute the target speed using the IDM
        target_speed = self._get_speed_idm(
            ego_speed,
            target_speed,
            0.0,
            0.0,
            distance_to_traffic_light,
            s0=self.configs.idm.red_light_minimum_distance,
            T=self.configs.idm.red_light_desired_time_headway,
        )

        return target_speed

    def _get_speed_affected_by_stop_sign(
        self, target_speed, next_stop_sign, actor_list
    ):
        """Handles the behavior of the ego vehicle when approaching a stop sign.

        Args:
            target_speed (float): The current target speed of the ego vehicle.
            next_stop_sign (carla.TrafficSign or None): The next stop sign in the route.
            actor_list (list): A list of all actors (vehicles, walkers, etc.) in the simulation.

        Returns:
            float: The adjusted target speed for the ego vehicle.
        """
        close_stop_signs = []
        ego_location = self.ego_vehicle.get_location()
        ego_speed = self.ego_vehicle.get_velocity().length()
        stop_signs = self.get_nearby_object(
            ego_location,
            actor_list.filter("*traffic.stop*"),
            self.configs.light_radius,
        )

        for stop_sign in stop_signs:
            center_bb_stop_sign = stop_sign.get_transform().transform(
                stop_sign.trigger_volume.location
            )

            stop_sign_extent = carla.Vector3D(1.5, 1.5, 0.5)
            bounding_box_stop_sign = carla.BoundingBox(
                center_bb_stop_sign, stop_sign_extent
            )
            rotation_stop_sign = stop_sign.get_transform().rotation
            bounding_box_stop_sign.rotation = carla.Rotation(
                pitch=rotation_stop_sign.pitch,
                yaw=rotation_stop_sign.yaw,
                roll=rotation_stop_sign.roll,
            )

            affects_ego = (
                next_stop_sign is not None
                and next_stop_sign.id == stop_sign.id
                and not self.cleared_stop_sign
            )
            close_stop_signs.append([bounding_box_stop_sign, stop_sign.id, affects_ego])

            if self.visualize:
                color = carla.Color(0, 1, 0) if affects_ego else carla.Color(1, 0, 0)
                self.world.debug.draw_box(
                    box=bounding_box_stop_sign,
                    rotation=bounding_box_stop_sign.rotation,
                    thickness=0.1,
                    color=color,
                    life_time=(1.0 / self.configs.carla_fps) + 1e-6,
                )

        if next_stop_sign is None:
            # No stop sign, continue with the current target speed
            return target_speed

        # Calculate the accurate distance to the stop sign
        distance_to_stop_sign = (
            next_stop_sign.get_transform()
            .transform(next_stop_sign.trigger_volume.location)
            .distance(ego_location)
        )

        # Reset the stop sign flag if we are farther than 10m away
        if distance_to_stop_sign > self.configs.unclearing_distance_to_stop_sign:
            self.cleared_stop_sign = False
        else:
            # Set the stop sign flag if we are closer than 3m and speed is low enough
            if (
                ego_speed < 0.1
                and distance_to_stop_sign < self.configs.clearing_distance_to_stop_sign
            ):
                self.cleared_stop_sign = True

        # Set the distance to stop sign as infinity if the stop sign has been cleared
        distance_to_stop_sign = (
            np.inf if self.cleared_stop_sign else distance_to_stop_sign
        )

        # Compute the target speed using the IDM
        target_speed = self._get_speed_idm(
            ego_speed,
            target_speed,
            0.0,
            0.0,
            distance_to_stop_sign,
            s0=self.configs.idm.stop_sign_minimum_distance,
            T=self.configs.idm.stop_sign_desired_time_headway,
        )

        # Return whether the ego vehicle is affected by the stop sign and the adjusted target speed
        return target_speed

    def _get_speed_brake_and_target(
        self,
        initial_target_speed,
        actor_list,
        vehicle_list,
        route_points,
        distance_to_next_traffic_light,
        next_traffic_light,
        distance_to_next_stop_sign,
        next_stop_sign,
        plant,
    ):
        """Compute the brake command and target speed for the ego vehicle based on various factors.

        Args:
            initial_target_speed (float): The initial target speed for the ego vehicle.
            actor_list (list): A list of all actors (vehicles, walkers, etc.) in the simulation.
            vehicle_list (list): A list of vehicle actors in the simulation.
            route_points (numpy.ndarray): An array of waypoints representing the planned route.
            plant (bool): Whether to use plant.
        """
        ego_speed = self.ego_vehicle.get_velocity().length()
        ego_location = self.ego_vehicle.get_location()
        ego_transform = self.ego_vehicle.get_transform()
        target_speed = initial_target_speed

        # Calculate the global bounding box of the ego vehicle
        center_ego_bb_global = ego_transform.transform(
            self.ego_vehicle.bounding_box.location
        )
        ego_bb_global = carla.BoundingBox(
            center_ego_bb_global, self.ego_vehicle.bounding_box.extent
        )
        ego_bb_global.rotation = ego_transform.rotation

        if self.visualize:
            self.world.debug.draw_box(
                box=ego_bb_global,
                rotation=ego_bb_global.rotation,
                thickness=0.1,
                color=self.configs.ego_vehicle.bb_color,
                life_time=self.configs.draw_life_time,
            )

        # Reset hazard flags
        self._reset_flags()

        # Compute if there will be a lane change close
        near_lane_change = is_near_lane_change(ego_speed, route_points)

        # Compute the number of future frames to consider for collision detection
        num_future_frames = int(
            self.configs.bicycle_frame_rate
            * (
                self.configs.forecast_length_lane_change
                if near_lane_change
                else self.configs.default_forecast_length
            )
        )

        # Get future bounding boxes of walkers
        if not plant:
            nearby_walkers, nearby_walker_ids = self.forecast_walker(
                actor_list, ego_location, num_future_frames
            )

        # Forecast the ego vehicle's bounding boxes for the future frames
        ego_bbox = self.forecast_ego_agent(ego_speed, num_future_frames, route_points)

        # Forecast the ego vehicle's bounding boxes for the future frames
        predicted_bbox = self.predict_bbox(
            actor_list, near_lane_change, num_future_frames, plant
        )

        # Compute the leading and rear vehicle IDs
        leading_vehicle_ids = self.waypoint_planner.compute_leading_vehicles(
            vehicle_list, self.ego_vehicle.id
        )
        rear_vehicle_ids = self.waypoint_planner.compute_trailing_vehicles(
            vehicle_list, self.ego_vehicle.id
        )

        # Compute the target speed with respect to the leading vehicle
        (
            target_speed_leading,
            speed_reduced_by_obj,
        ) = self._get_speed_wrt_leading_vehicle(
            initial_target_speed,
            predicted_bbox,
            near_lane_change,
            leading_vehicle_ids,
            rear_vehicle_ids,
            speed_reduced_by_obj,
            plant,
        )

        # Compute the target speeds with respect to all actors (vehicles, bicycles, walkers)
        (
            target_speed_bicycle,
            target_speed_walker,
            target_speed_vehicle,
            speed_reduced_by_obj,
        ) = self._get_speed_wrt_all_actors(
            initial_target_speed,
            ego_bbox,
            predicted_bbox,
            near_lane_change,
            leading_vehicle_ids,
            rear_vehicle_ids,
            speed_reduced_by_obj,
            nearby_walkers,
            nearby_walker_ids,
        )

        # Compute the target speed with respect to the red light
        target_speed_traffic_light = self._get_speed_affected_by_traffic_light(
            initial_target_speed, distance_to_next_traffic_light, next_traffic_light
        )

        # Update the object causing the most speed reduction
        if (
            speed_reduced_by_obj is None
            or speed_reduced_by_obj[0] > target_speed_traffic_light
        ):
            speed_reduced_by_obj = [
                target_speed_traffic_light,
                None if next_traffic_light is None else next_traffic_light.type_id,
                None if next_traffic_light is None else next_traffic_light.id,
                distance_to_next_traffic_light,
            ]

        # Compute the target speed with respect to the stop sign
        target_speed_stop_sign = self._get_speed_affected_by_stop_sign(
            initial_target_speed, next_stop_sign, actor_list
        )

        # Update the object causing the most speed reduction
        if (
            speed_reduced_by_obj is None
            or speed_reduced_by_obj[0] > target_speed_stop_sign
        ):
            speed_reduced_by_obj = [
                target_speed_stop_sign,
                None if next_stop_sign is None else next_stop_sign.type_id,
                None if next_stop_sign is None else next_stop_sign.id,
                distance_to_next_stop_sign,
            ]

        # Compute the minimum target speed considering all factors
        target_speed = min(
            target_speed_leading,
            target_speed_bicycle,
            target_speed_vehicle,
            target_speed_walker,
            target_speed_traffic_light,
            target_speed_stop_sign,
        )

        # Set the hazard flags based on the target speed and its cause
        if (
            target_speed == target_speed_walker
            and target_speed_walker != initial_target_speed
        ):
            self.walker_hazard = True
            self.walker_close = True
        elif (
            target_speed == target_speed_traffic_light
            and target_speed_traffic_light != initial_target_speed
        ):
            self.traffic_light_hazard = True
        elif (
            target_speed == target_speed_stop_sign
            and target_speed_stop_sign != initial_target_speed
        ):
            self.stop_sign_hazard = True
            self.stop_sign_close = True

        # Determine if the ego vehicle needs to brake based on the target speed
        brake = target_speed == 0
        return brake, target_speed, speed_reduced_by_obj

    def _get_steer(
        self, route_points, current_position, current_heading, current_speed
    ):
        """Calculate the steering angle based on the current position, heading, speed, and the route points.

        Args:
            route_points (numpy.ndarray): An array of (x, y) coordinates representing the route points.
            current_position (tuple): The current position (x, y) of the vehicle.
            current_heading (float): The current heading angle (in radians) of the vehicle.
            current_speed (float): The current speed of the vehicle (in m/s).

        Returns:
            float: The calculated steering angle.
        """
        speed_scale = self.configs.lateral_pid.speed_scale
        speed_offset = self.configs.lateral_pid.speed_offset

        # Calculate the lookahead index based on the current speed
        speed_in_kmph = current_speed * 3.6
        lookahead_distance = speed_scale * speed_in_kmph + speed_offset
        lookahead_distance = np.clip(
            lookahead_distance,
            self.configs.lateral_pid.default_lookahead,
            self.configs.lateral_pid.maximum_lookahead_distance,
        )
        lookahead_index = int(min(lookahead_distance, route_points.shape[0] - 1))

        # Get the target point from the route points
        self.aim_wp = route_points[lookahead_index]

        # Calculate the angle between the current heading and the target point
        self.angle = get_angle_by_position(
            current_position, current_heading, self.aim_wp
        )

        # Calculate the steering angle using the turn controller
        steering_angle = self.lateral_controller.step(
            route_points, current_speed, current_position, current_heading
        )
        steering_angle = round(steering_angle, 3)

        return steering_angle

    def get_control(self, input_data):
        ego_gps, ego_speed, ego_compass = self.get_ego_state(input_data)

        # Waypoint planning and route generation
        (
            route_np,
            route_wp,
            _,
            distance_to_next_traffic_light,
            next_traffic_light,
            distance_to_next_stop_sign,
            next_stop_sign,
            speed_limit,
        ) = self.waypoint_planner.run_step(ego_gps)

        # Extract relevant route information
        self.remaining_route = route_np[self.configs.tf_first_checkpoint_distance :][
            :: self.configs.points_per_meter
        ]
        self.remaining_route_original = self.waypoint_planner.original_route_points[
            self.waypoint_planner.route_index :
        ][self.configs.tf_first_checkpoint_distance :][:: self.configs.points_per_meter]

        target_speed = speed_limit * self.configs.ratio_target_speed_limit

        # Reduce target speed if there is a junction ahead
        for i in range(
            min(self.configs.max_lookahead_to_check_for_junction, len(route_wp))
        ):
            if route_wp[i].is_junction:
                target_speed = min(target_speed, self.configs.max_speed_in_junction)
                break

        # Get the list of vehicles in the scene
        actors = self.world.get_actors()
        vehicles = list(actors.filter("*vehicle*"))

        # Manage route obstacle scenarios and adjust target speed
        (
            target_speed_route_obstacle,
            keep_driving,
            speed_reduced_by_obj,
        ) = self._manage_route_obstacle_scenarios(
            target_speed, ego_speed, route_wp, vehicles, route_np
        )

        # In case the agent overtakes an obstacle, keep driving in case the opposite lane is free instead of using idm
        # and the kinematic bicycle model forecasts
        if keep_driving:
            brake, target_speed = False, target_speed_route_obstacle
        else:
            brake, target_speed, speed_reduced_by_obj = self.get_speed_brake_and_target(
                target_speed,
            )

        target_speed = min(target_speed, target_speed_route_obstacle)

        # Determine if the ego vehicle is at a junction
        ego_waypoint = self.world_map.get_waypoint(self.ego_vehicle.get_location())
        self.is_junction = ego_waypoint.is_junction

        # Compute throttle and brake control
        throttle, control_brake = self.longitudinal_controller.get_throttle_and_brake(
            brake, target_speed, ego_speed
        )

        # Compute steering control
        steer = self._get_steer(route_np, ego_gps, ego_compass, ego_speed)

        # Create the control command
        control = carla.VehicleControl()
        control.steer = steer + self.configs.steer_noise * np.random.randn()
        control.throttle = throttle
        control.brake = float(brake or control_brake)

        # Apply brake if the vehicle is stopped to prevent rolling back
        if (
            control.throttle == 0
            and ego_speed < self.configs.minimum_speed_to_prevent_rolling_back
        ):
            control.brake = 1

        # Apply throttle if the vehicle is blocked for too long
        ego_velocity = CarlaDataProvider.get_velocity(self.ego_vehicle)
        if ego_velocity < 0.1:
            self.ego_blocked_for_ticks += 1
        else:
            self.ego_blocked_for_ticks = 0

        if self.ego_blocked_for_ticks >= self.configs.max_blocked_ticks:
            control.throttle = 1
            control.brake = 0

        # Save control commands and target speed
        self.steer = control.steer
        self.throttle = control.throttle
        self.brake = control.brake
        self.target_speed = target_speed

        # Update speed histogram if enabled
        if self.make_histogram:
            self.speed_histogram.append((self.target_speed * 3.6) if not brake else 0.0)

        # Get the target and next target points from the command planner
        command_route = self._command_planner.run_step(ego_gps)
        if len(command_route) > 2:
            target_point, far_command = command_route[1]
            next_target_point, next_far_command = command_route[2]
        elif len(command_route) > 1:
            target_point, far_command = command_route[1]
            next_target_point, next_far_command = command_route[1]
        else:
            target_point, far_command = command_route[0]
            next_target_point, next_far_command = command_route[0]

        # Update command history and save driving datas
        if (target_point != self.target_point_prev).all():
            self.target_point_prev = target_point
            self.commands.append(far_command.value)
            self.next_commands.append(next_far_command.value)

        return control

    def predict_bbox(self, actor_list, near_lane_change, num_future_frames, plant):
        """Predict the future bounding boxes of actors for a given number of frames.

        Args:
            actor_list (list): A list of actors (e.g., vehicles) in the simulation.
            near_lane_change (bool): Whether the ego vehicle is near a lane change maneuver.
            num_future_frames (int): The number of future frames to predict.
            plant (bool): Whether to use plant.

        Returns:
            dict: A dictionary mapping actor IDs to lists of predicted bounding boxes for each future frame.
        """
        predicted_bounding_boxes = {}
        ego_location = self.ego_vehicle.get_location()

        if not plant:
            # Filter out nearby actors within the detection radius, excluding the ego vehicle
            nearby_actors = [
                actor
                for actor in actor_list
                if actor.id != self.ego_vehicle.id
                and actor.get_location().distance(ego_location)
                < self.configs.detection_radius
            ]

            # If there are nearby actors, calculate their future bounding boxes
            if nearby_actors:
                # Get the previous control inputs (steering, throttle, brake) for the nearby actors
                previous_controls = [actor.get_control() for actor in nearby_actors]
                previous_actions = np.array(
                    [
                        [control.steer, control.throttle, control.brake]
                        for control in previous_controls
                    ]
                )

                # Get the current velocities, locations, and headings of the nearby actors
                velocities = np.array(
                    [actor.get_velocity().length() for actor in nearby_actors]
                )
                locations = np.array(
                    [
                        [
                            actor.get_location().x,
                            actor.get_location().y,
                            actor.get_location().z,
                        ]
                        for actor in nearby_actors
                    ]
                )
                headings = np.deg2rad(
                    np.array(
                        [actor.get_transform().rotation.yaw for actor in nearby_actors]
                    )
                )

                # Initialize arrays to store future locations, headings, and velocities
                future_locations = np.empty(
                    (num_future_frames, len(nearby_actors), 3), dtype="float"
                )
                future_headings = np.empty(
                    (num_future_frames, len(nearby_actors)), dtype="float"
                )
                future_velocities = np.empty(
                    (num_future_frames, len(nearby_actors)), dtype="float"
                )

                # Forecast the future locations, headings, and velocities for the nearby actors
                for i in range(num_future_frames):
                    (
                        locations,
                        headings,
                        velocities,
                    ) = self.vehicle_model.forecast_other_vehicles(
                        locations, headings, velocities, previous_actions
                    )
                    future_locations[i] = locations.copy()
                    future_velocities[i] = velocities.copy()
                    future_headings[i] = headings.copy()

                # Convert future headings to degrees
                future_headings = np.rad2deg(future_headings)

                # Calculate the predicted bounding boxes for each nearby actor and future frame
                for actor_idx, actor in enumerate(nearby_actors):
                    predicted_actor_boxes = []

                    for i in range(num_future_frames):
                        # Calculate the future location of the actor
                        location = carla.Location(
                            x=future_locations[i, actor_idx, 0].item(),
                            y=future_locations[i, actor_idx, 1].item(),
                            z=future_locations[i, actor_idx, 2].item(),
                        )

                        # Calculate the future rotation of the actor
                        rotation = carla.Rotation(
                            pitch=0, yaw=future_headings[i, actor_idx], roll=0
                        )

                        # Get the extent (dimensions) of the actor's bounding box
                        extent = actor.bounding_box.extent
                        # Otherwise we would increase the extent of the bounding box of the vehicle
                        extent = carla.Vector3D(x=extent.x, y=extent.y, z=extent.z)

                        # Adjust the bounding box size based on velocity and lane change maneuver to adjust for
                        # uncertainty during forecasting
                        s = (
                            self.configs.high_speed_min_extent_x_other_vehicle_lane_change
                            if near_lane_change
                            else self.configs.high_speed_min_extent_x_other_vehicle
                        )
                        extent.x *= (
                            self.configs.slow_speed_extent_factor_ego
                            if future_velocities[i, actor_idx]
                            < self.configs.extent_other_vehicles_bbs_speed_threshold
                            else max(
                                s,
                                self.configs.high_speed_min_extent_x_other_vehicle
                                * float(i)
                                / float(num_future_frames),
                            )
                        )
                        extent.y *= (
                            self.configs.slow_speed_extent_factor_ego
                            if future_velocities[i, actor_idx]
                            < self.configs.extent_other_vehicles_bbs_speed_threshold
                            else max(
                                self.configs.high_speed_min_extent_y_other_vehicle,
                                self.configs.high_speed_extent_y_factor_other_vehicle
                                * float(i)
                                / float(num_future_frames),
                            )
                        )

                        # Create the bounding box for the future frame
                        bounding_box = carla.BoundingBox(location, extent)
                        bounding_box.rotation = rotation

                        # Append the bounding box to the list of predicted bounding boxes for this actor
                        predicted_actor_boxes.append(bounding_box)

                    # Store the predicted bounding boxes for this actor in the dictionary
                    predicted_bounding_boxes[actor.id] = predicted_actor_boxes

                if self.visualize:
                    for (
                        actor_idx,
                        actors_forecasted_bounding_boxes,
                    ) in predicted_bounding_boxes.items():
                        for bb in actors_forecasted_bounding_boxes:
                            self.world.debug.draw_box(
                                box=bb,
                                rotation=bb.rotation,
                                thickness=0.1,
                                color=self.configs.other_vehicles_forecasted_bbs_color,
                                life_time=self.configs.draw_life_time,
                            )

        return predicted_bounding_boxes

    def forecast_ego_agent(self, initial_target_speed, num_future_frames, route_points):
        """Forecast the future states of the ego agent using the kinematic bicycle model and assume their is no hazard to check subsequently whether the ego vehicle would collide.

        Args:
            initial_target_speed (float): The initial target speed for the ego vehicle.
            num_future_frames (int): The number of future frames to forecast.
            route_points (numpy.ndarray): An array of waypoints representing the planned route.

        Returns:
            list: A list of bounding boxes representing the future states of the ego vehicle.
        """
        ego_transform = self.ego_vehicle.get_transform()
        ego_speed = self.ego_vehicle.get_velocity().length()

        self.lateral_controller.save()
        self.waypoint_planner.save()

        # Initialize the initial state without braking
        location = np.array(
            [
                ego_transform.location.x,
                ego_transform.location.y,
                ego_transform.location.z,
            ]
        )

        heading_angle = np.deg2rad(ego_transform.rotation.yaw)
        speed = ego_speed

        # Calculate the throttle command based on the target speed and current speed
        throttle = self.longitudinal_controller.get_throttle_extrapolation(
            initial_target_speed, ego_speed
        )
        steering = self.lateral_controller.step(
            route_points, speed, location, heading_angle.item()
        )
        action = np.array([steering, throttle, 0.0]).flatten()

        future_bounding_boxes = []
        # Iterate over the future frames and forecast the ego agent's state
        for _ in range(num_future_frames):
            # Forecast the next state using the kinematic bicycle model
            (
                location,
                heading_angle,
                speed,
            ) = self.ego_physics_model.forecast_ego_vehicle(
                location, heading_angle, speed, action
            )

            # Update the route and extrapolate steering and throttle commands
            extrapolated_route, _, _, _, _, _, _, _ = self.waypoint_planner.run_step(
                location
            )
            steering = self.lateral_controller.step(
                extrapolated_route, speed, location, heading_angle.item()
            )
            throttle = self.longitudinal_controller.get_throttle_extrapolation(
                initial_target_speed, speed
            )
            action = np.array([steering, throttle, 0.0]).flatten()

            heading_angle_degrees = np.rad2deg(heading_angle).item()

            # Decrease the ego vehicles bounding box if it is slow and resolve permanent bounding box intersections at collisions.
            # In case of driving increase them for safety.
            extent = self.ego_vehicle.bounding_box.extent
            # Otherwise we would increase the extent of the bounding box of the vehicle
            extent = carla.Vector3D(x=extent.x, y=extent.y, z=extent.z)
            extent.x *= (
                self.configs.slow_speed_extent_factor_ego
                if ego_speed < self.configs.extent_ego_bbs_speed_threshold
                else self.configs.high_speed_extent_factor_ego_x
            )
            extent.y *= (
                self.configs.slow_speed_extent_factor_ego
                if ego_speed < self.configs.extent_ego_bbs_speed_threshold
                else self.configs.high_speed_extent_factor_ego_y
            )

            transform = carla.Transform(
                carla.Location(
                    x=location[0].item(), y=location[1].item(), z=location[2].item()
                )
            )

            ego_bounding_box = carla.BoundingBox(transform.location, extent)
            ego_bounding_box.rotation = carla.Rotation(
                pitch=0, yaw=heading_angle_degrees, roll=0
            )

            future_bounding_boxes.append(ego_bounding_box)

        self.lateral_controller.load_state()
        self.waypoint_planner.load()

        return future_bounding_boxes

    def forecast_walker(self, actors, number_of_future_frames):
        """
        Forecast the future locations of walkers in the vicinity of the ego vehicle assuming they
        keep their velocity and direction

        Args:
            actors (carla.ActorList): A list of actors in the simulation.
            number_of_future_frames (int): The number of future frames to forecast.

        Returns:
            tuple: A tuple containing two lists:
                - list: A list of lists, where each inner list contains the future bounding boxes for a walker.
                - list: A list of IDs for the walkers whose locations were forecasted.
        """
        ego_location = self.ego_vehicle.get_location()
        nearby_walkers_bbs, nearby_walker_ids = [], []

        # Filter walkers within the detection radius
        walkers = [
            walker
            for walker in actors.filter("*walker*")
            if walker.get_location().distance(ego_location)
            < self.configs.detection_radius
        ]

        # If no walkers are found, return empty lists
        if not walkers:
            return nearby_walkers_bbs, nearby_walker_ids

        # Extract walker locations, speeds, and directions
        walker_locations = np.array(
            [
                [
                    walker.get_location().x,
                    walker.get_location().y,
                    walker.get_location().z,
                ]
                for walker in walkers
            ]
        )
        walker_speeds = np.array([walker.get_velocity().length() for walker in walkers])
        walker_speeds = np.maximum(walker_speeds, self.configs.min_walker_speed)
        walker_directions = np.array(
            [
                [
                    walker.get_control().direction.x,
                    walker.get_control().direction.y,
                    walker.get_control().direction.z,
                ]
                for walker in walkers
            ]
        )

        # Calculate future walker locations based on their current locations, speeds, and directions
        future_walker_locations = (
            walker_locations[:, None, :]
            + np.arange(1, number_of_future_frames + 1)[None, :, None]
            * walker_directions[:, None, :]
            * walker_speeds[:, None, None]
            / self.configs.bicycle_frame_rate
        )

        # Iterate over walkers and calculate their future bounding boxes
        for i, walker in enumerate(walkers):
            bb, transform = walker.bounding_box, walker.get_transform()
            rotation = carla.Rotation(
                pitch=bb.rotation.pitch + transform.rotation.pitch,
                yaw=bb.rotation.yaw + transform.rotation.yaw,
                roll=bb.rotation.roll + transform.rotation.roll,
            )
            extent = bb.extent
            extent.x = max(
                self.configs.walker_minimum_extent, extent.x
            )  # Ensure a minimum width
            extent.y = max(
                self.configs.walker_minimum_extent, extent.y
            )  # Ensure a minimum length

            walker_future_bboxes = []
            for j in range(number_of_future_frames):
                location = carla.Location(
                    future_walker_locations[i, j, 0],
                    future_walker_locations[i, j, 1],
                    future_walker_locations[i, j, 2],
                )

                bounding_box = carla.BoundingBox(location, extent)
                bounding_box.rotation = rotation
                walker_future_bboxes.append(bounding_box)

            nearby_walker_ids.append(walker.id)
            nearby_walkers_bbs.append(walker_future_bboxes)

        # Visualize the future bounding boxes of walkers (if enabled)
        if self.visualize:
            for bbs in nearby_walkers_bbs:
                for bbox in bbs:
                    self.world.debug.draw_box(
                        box=bbox,
                        rotation=bbox.rotation,
                        thickness=0.1,
                        color=self.configs.walker_forecasted_bbs_color,
                        life_time=self.configs.draw_life_time,
                    )

        return nearby_walkers_bbs, nearby_walker_ids
