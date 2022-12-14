from collections import defaultdict
from enum import Enum
import argparse
import copy
import pdb
import time
from typing import Optional, Iterable, List, Dict
from dataclasses import dataclass

import numpy as np
import sophus as sp
import rospy
from std_srvs.srv import Trigger, TriggerRequest
from std_srvs.srv import SetBool, SetBoolRequest
from geometry_msgs.msg import PoseStamped, Pose, Twist
import actionlib
from control_msgs.msg import FollowJointTrajectoryAction
from control_msgs.msg import FollowJointTrajectoryGoal
from trajectory_msgs.msg import JointTrajectoryPoint
from sensor_msgs.msg import JointState

from home_robot.utils.geometry import xyt2sophus, sophus2xyt, xyt_base_to_global
from home_robot.utils.geometry.ros import pose_sophus2ros, pose_ros2sophus


T_LOC_STABILIZE = 1.0
T_GOAL_TIME_TOL = 1.0

ROS_BASE_TRANSLATION_JOINT = "translate_mobile_base"
ROS_ARM_JOINT = "joint_arm"
ROS_LIFT_JOINT = "joint_lift"
ROS_WRIST_YAW = "joint_wrist_yaw"
ROS_WRIST_PITCH = "joint_wrist_pitch"
ROS_WRIST_ROLL = "joint_wrist_roll"
ROS_GRIPPER_FINGER = "joint_gripper_finger_left"  # used to control entire gripper
ROS_HEAD_PAN = "joint_head_pan"
ROS_HEAD_TILT = "joint_head_tilt"

ROS_ARM_JOINTS_ACTUAL = ["joint_arm_l0", "joint_arm_l1", "joint_arm_l2", "joint_arm_l3"]

STRETCH_GRIPPER_OPEN = 0.22
STRETCH_GRIPPER_CLOSE = -0.2


@dataclass
class ManipulatorBaseParams:
    se3_base: sp.SE3


class ControlMode(Enum):
    IDLE = 0
    VELOCITY = 1
    NAVIGATION = 2
    MANIPULATION = 3


def limit_control_mode(valid_modes: List[ControlMode]):
    """Decorator for checking if a robot method is executed while the correct mode is present."""

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            if self._robot_state.base_control_mode in valid_modes:
                return func(self, *args, **kwargs)
            else:
                rospy.logwarn(
                    f"'{func.__name__}' is only available in the following modes: {valid_modes}"
                )
                rospy.logwarn(f"Current mode is: {self._control_mode}")
                return None

        return wrapper

    return decorator


@dataclass
class StretchRobotState:
    """
    Minimum representation of the state of the robot
    """

    base_control_mode: ControlMode

    last_base_update_timestamp: rospy.Time
    t_base: sp.SE3

    last_joint_update_timestamp: rospy.Time
    q_lift: float
    q_arm: float
    q_wrist_yaw: float
    q_wrist_pitch: float
    q_wrist_roll: float
    q_gripper_finger: float
    q_head_pan: float
    q_head_tilt: float


class LocalHelloRobot:
    """
    ROS interface for robot base control
    Currently only works with a local rosmaster
    """

    def __init__(self, init_node: bool = True):
        self._robot_state = StretchRobotState(base_control_mode=ControlMode.IDLE)

        # Ros pubsub
        if init_node:
            rospy.init_node("user")

        self._goal_pub = rospy.Publisher("goto_controller/goal", Pose, queue_size=1)
        self._velocity_pub = rospy.Publisher("stretch/cmd_vel", Twist, queue_size=1)

        self._base_state_sub = rospy.Subscriber(
            "state_estimator/pose_filtered",
            PoseStamped,
            self._base_state_callback,
            queue_size=1,
        )
        self._joint_state_sub = rospy.Subscriber(
            "stretch/joint_states",
            JointState,
            self._joint_state_callback,
            queue_size=1,
        )

        self._nav_mode_service = rospy.ServiceProxy(
            "switch_to_navigation_mode", Trigger
        )
        self._pos_mode_service = rospy.ServiceProxy("switch_to_position_mode", Trigger)

        self._goto_on_service = rospy.ServiceProxy("goto_controller/enable", Trigger)
        self._goto_off_service = rospy.ServiceProxy("goto_controller/disable", Trigger)
        self._set_yaw_service = rospy.ServiceProxy(
            "goto_controller/set_yaw_tracking", SetBool
        )

        self.trajectory_client = actionlib.SimpleActionClient(
            "/stretch_controller/follow_joint_trajectory", FollowJointTrajectoryAction
        )

        # Initialize control mode & home robot
        self.switch_to_manipulation_mode()
        self.close_gripper()
        self.set_arm_joint_positions([0.1, 0.3, 0, 0, 0, 0])
        self._robot_state.base_control_mode = ControlMode.IDLE

    # Getter interfaces
    def get_robot_state(self):
        """
        Note: read poses from tf2 buffer

        base
            pose
                pos
                quat
            pose_se2
            twist_se2
        arm
            joint_positions
            ee
                pose
                    pos
                    quat
        head
            joint_positions
                pan
                tilt
            pose
                pos
                quat
        """
        robot_state = copy.copy(self._robot_state)
        output = defaultdict(dict)

        # Base state
        output["base"]["pose_se2"] = sophus2xyt(robot_state.t_base)
        output["base"]["twist_se2"] = np.zeros(3)

        # Manipulator states
        output["joint_positions"] = np.array(
            [
                self._compute_base_translation_pos(robot_state.t_base),
                robot_state.q_lift,
                robot_state.q_arm,
                robot_state.q_wrist_yaw,
                robot_state.q_wrist_pitch,
                robot_state.q_wrist_roll,
            ]
        )

        # Head states
        output["head"]["pan"] = robot_state.q_head_pan
        output["head"]["tilt"] = robot_state.q_head_tilt

        return output

    def get_base_state(self):
        return self.get_robot_state["base"]

    def get_camera_image(self):
        """
        rgb, depth, xyz = self.robot.get_images()
        return rgb, depth
        """
        pass

    def get_joint_limits(self):
        """
        arm
            max
            min
        head
            pan
                max
                min
            tilt
                max
                min
        """
        raise NotImplementedError

    def get_ee_limits(self):
        """
        max
        min
        """
        raise NotImplementedError

    # Mode switching interfaces
    def switch_to_velocity_mode(self):
        result1 = self._nav_mode_service(TriggerRequest())
        result2 = self._goto_off_service(TriggerRequest())

        # Switch interface mode & print messages
        self._robot_state.base_control_mode = ControlMode.VELOCITY
        rospy.loginfo(result1.message)
        rospy.loginfo(result2.message)

        return result1.success and result2.success

    def switch_to_navigation_mode(self):
        result1 = self._nav_mode_service(TriggerRequest())
        result2 = self._goto_on_service(TriggerRequest())

        # Switch interface mode & print messages
        self._robot_state.base_control_mode = ControlMode.NAVIGATION
        rospy.loginfo(result1.message)
        rospy.loginfo(result2.message)

        return result1.success and result2.success

    def switch_to_manipulation_mode(self):
        result1 = self._pos_mode_service(TriggerRequest())
        result2 = self._goto_off_service(TriggerRequest())

        # Wait for navigation to stabilize
        rospy.sleep(T_LOC_STABILIZE)

        # Set manipulator params
        self._manipulator_params = ManipulatorBaseParams(
            se3_base=self._robot_state.t_base,
        )

        # Switch interface mode & print messages
        self._robot_state.bsae_control_mode = ControlMode.MANIPULATION
        rospy.loginfo(result1.message)
        rospy.loginfo(result2.message)

        return result1.success and result2.success

    # Control interfaces
    @limit_control_mode([ControlMode.VELOCITY])
    def set_velocity(self, v, w):
        """
        Directly sets the linear and angular velocity of robot base.
        """
        msg = Twist()
        msg.linear.x = v
        msg.angular.z = w
        self._velocity_pub.publish(msg)

    @limit_control_mode([ControlMode.NAVIGATION])
    def navigate_to(
        self,
        xyt: Iterable[float],
        relative: bool = False,
        position_only: bool = False,
        avoid_obstacles: bool = False,
    ):
        """
        Cannot be used in manipulation mode.
        """
        # Parse inputs
        assert len(xyt) == 3, "Input goal location must be of length 3."

        if avoid_obstacles:
            raise NotImplementedError("Obstacle avoidance unavailable.")

        # Set yaw tracking
        self._set_yaw_service(SetBoolRequest(data=(not position_only)))

        # Compute absolute goal
        if relative:
            xyt_base = self.get_base_state()["pose_se2"]
            xyt_goal = xyt_base_to_global(xyt, xyt_base)
        else:
            xyt_goal = xyt

        # Set goal
        msg = pose_sophus2ros(xyt2sophus(xyt_goal))
        self._goal_pub.publish(msg)

    @limit_control_mode([ControlMode.MANIPULATION])
    def set_arm_joint_positions(self, joint_positions: Iterable[float]):
        """
        list of robot arm joint positions:
            BASE_TRANSLATION = 0
            LIFT = 1
            ARM = 2
            WRIST_YAW = 3
            WRIST_PITCH = 4
            WRIST_ROLL = 5
        """
        assert len(joint_positions) == 6, "Joint position vector must be of length 6."

        # Preprocess base translation joint position (command is actually delta position)
        base_joint_pos_curr = self._compute_base_translation_pos()
        base_joint_pos_cmd = joint_positions[0] - base_joint_pos_curr

        # Construct and send command
        joint_goals = {
            ROS_BASE_TRANSLATION_JOINT: base_joint_pos_cmd,
            ROS_LIFT_JOINT: joint_positions[1],
            ROS_ARM_JOINT: joint_positions[2],
            ROS_WRIST_YAW: joint_positions[3],
            ROS_WRIST_PITCH: joint_positions[4],
            ROS_WRIST_ROLL: joint_positions[5],
        }

        self._send_ros_trajectory_goals(joint_goals)

        return True

    @limit_control_mode([ControlMode.MANIPULATION])
    def set_ee_pose(
        self,
        pos: Iterable[float],
        quat: Optional[Iterable[float]] = None,
        relative: bool = False,
    ):
        """
        Does not rotate base.
        Cannot be used in navigation mode.
        """
        # TODO: check pose
        raise NotImplementedError

    @limit_control_mode(
        [
            ControlMode.VELOCITY,
            ControlMode.NAVIGATION,
            ControlMode.MANIPULATION,
        ]
    )
    def open_gripper(self):
        self._send_ros_trajectory_goals({ROS_GRIPPER_FINGER: STRETCH_GRIPPER_OPEN})

    @limit_control_mode(
        [
            ControlMode.VELOCITY,
            ControlMode.NAVIGATION,
            ControlMode.MANIPULATION,
        ]
    )
    def close_gripper(self):
        self._send_ros_trajectory_goals({ROS_GRIPPER_FINGER: STRETCH_GRIPPER_CLOSE})

    @limit_control_mode(
        [
            ControlMode.VELOCITY,
            ControlMode.NAVIGATION,
            ControlMode.MANIPULATION,
        ]
    )
    def set_camera_pan_tilt(
        self, pan: Optional[float] = None, tilt: Optional[float] = None
    ):
        joint_goals = {}
        if pan is not None:
            joint_goals[ROS_HEAD_PAN] = pan
        if tilt is not None:
            joint_goals[ROS_HEAD_TILT] = tilt

        self._send_ros_trajectory_goals(joint_goals)

    @limit_control_mode(
        [
            ControlMode.VELOCITY,
            ControlMode.NAVIGATION,
            ControlMode.MANIPULATION,
        ]
    )
    def set_camera_pose(self, pose_so3):
        raise NotImplementedError  # TODO

    @limit_control_mode([ControlMode.NAVIGATION])
    def navigate_to_camera_pose(self, pose_se3):
        # Compute base pose
        # Navigate to base pose
        # Perform camera pan/tilt
        raise NotImplementedError  # TODO

    # Helper functions
    def _send_ros_trajectory_goals(self, joint_goals: Dict[str, float]):
        # Preprocess arm joints (arm joints are actually 4 joints in one)
        if ROS_ARM_JOINT in joint_goals:
            arm_joint_goal = joint_goals.pop(ROS_ARM_JOINT)

            for arm_joint_name in ROS_ARM_JOINTS_ACTUAL:
                joint_goals[arm_joint_name] = arm_joint_goal / len(
                    ROS_ARM_JOINTS_ACTUAL
                )

        # Preprocess base translation joint (stretch_driver errors out if translation value is 0)
        if ROS_BASE_TRANSLATION_JOINT in joint_goals:
            if joint_goals[ROS_BASE_TRANSLATION_JOINT] == 0:
                joint_goals.pop(ROS_BASE_TRANSLATION_JOINT)

        # Parse input
        joint_names = []
        joint_values = []
        for name, val in joint_goals.items():
            joint_names.append(name)
            joint_values.append(val)

        # Construct goal positions
        point_msg = JointTrajectoryPoint()
        point_msg.positions = joint_values

        # Construct goal msg
        goal_msg = FollowJointTrajectoryGoal()
        goal_msg.goal_time_tolerance = rospy.Time(T_GOAL_TIME_TOL)
        goal_msg.trajectory.joint_names = joint_names
        goal_msg.trajectory.points = [point_msg]
        goal_msg.trajectory.header.stamp = rospy.Time.now()

        # Send goal
        self.trajectory_client.send_goal(goal_msg)

    def _compute_base_translation_pos(self, t_base=None):
        if self._robot_state.base_control_mode != ControlMode.MANIPULATION:
            return 0.0

        l0_pose = self._manipulator_params.se3_base
        l1_pose = self._robot_state.t_base if t_base is None else t_base
        return (l0_pose.inverse() * l1_pose).translation()[0]

    # Subscriber callbacks
    def _base_state_callback(self, msg: PoseStamped):
        self._robot_state.last_base_update_timestamp = msg.header.stamp
        self._robot_state.t_base = pose_ros2sophus(msg.pose)

    def _joint_state_callback(self, msg: JointState):
        self._robot_state.last_joint_update_timestamp = msg.header.stamp

        if ROS_ARM_JOINTS_ACTUAL[0] in msg.names:
            self._robot_state.q_arm = 0.0

        for name, pos in zip(msg.names, msg.position):
            if name == ROS_LIFT_JOINT:
                self._robot_state.q_lift = pos
            elif name in ROS_ARM_JOINTS_ACTUAL:
                self._robot_state.q_arm += pos
            elif name == ROS_WRIST_YAW:
                self._robot_state.q_wrist_yaw = pos
            elif name == ROS_WRIST_PITCH:
                self._robot_state.q_wrist_pitch = pos
            elif name == ROS_WRIST_ROLL:
                self._robot_state.q_wrist_roll = pos
            elif name == ROS_GRIPPER_FINGER:
                self._robot_state.q_gripper_finger = pos
            elif name == ROS_HEAD_PAN:
                self._robot_state.q_head_pan = pos
            elif name == ROS_HEAD_TILT:
                self._robot_state.q_head_tilt = pos


if __name__ == "__main__":
    # Launches an interactive terminal if file is directly run
    robot = LocalHelloRobot()

    import code

    code.interact(local=locals())
