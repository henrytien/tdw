from typing import List, Dict
from tdw.output_data import OutputData, StaticRobot
from tdw.add_ons.agents.robot_data.joint_static import JointStatic
from tdw.add_ons.agents.robot_data.non_moving import NonMoving


class RobotStatic:
    """
    Static data for a robot that won't change due to physics (such as the joint IDs, segmentation colors, etc.)
    """

    def __init__(self, robot_id: int, resp: List[bytes]):
        """
        :param resp: The response from the build, which we assume contains `Robot` output data.
        :param robot_id: The ID of this robot.
        """

        """:field
        The ID of the robot.
        """
        self.robot_id: int = robot_id
        """:field
        A dictionary of [Static robot joint data](joint_static.md) for each joint. Key = The ID of the joint.
        """
        self.joints: Dict[int, JointStatic] = dict()
        """:field
        A dictionary of joint names. Key = The name of the joint. Value = The joint ID.
        """
        self.joint_names: Dict[str, int] = dict()
        """:field
        A dictionary of [Static data for non-moving parts](non_moving.md) for each non-moving part. Key = The ID of the part.
        """
        self.non_moving: Dict[int, NonMoving] = dict()
        """:field
        A list of joint IDs and non-moving body part IDs.
        """
        self.body_parts: List[int] = list()
        """:field
        If True, the robot is immovable.
        """
        self.immovable: bool = False
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            if r_id == "srob":
                static_robot: StaticRobot = StaticRobot(resp[i])
                if static_robot.get_id() == robot_id:
                    for j in range(static_robot.get_num_joints()):
                        joint = JointStatic(static_robot=static_robot, joint_index=j)
                        self.joints[joint.joint_id] = joint
                        self.joint_names[joint.name] = joint.joint_id
                        if joint.root:
                            self.immovable = joint.immovable
                    for j in range(static_robot.get_num_non_moving()):
                        non_moving = NonMoving(static_robot=static_robot, index=j)
                        self.non_moving[non_moving.object_id] = non_moving
        self.body_parts: List[int] = list(self.joints.keys())
        self.body_parts.extend(self.non_moving.keys())
