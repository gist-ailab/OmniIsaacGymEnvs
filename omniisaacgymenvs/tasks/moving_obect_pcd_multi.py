import torch
from torch import linalg as LA
import numpy as np
from copy import deepcopy
 
from omni.isaac.core.utils.extensions import enable_extension
enable_extension("omni.replicator.isaac")   # required for PytorchListener

# CuRobo
from curobo.geom.types import WorldConfig
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.types.robot import RobotConfig
from curobo.util_file import get_robot_configs_path, get_world_configs_path, join_path, load_yaml
from curobo.wrap.reacher.ik_solver import IKSolver, IKSolverConfig
torch.backends.cudnn.benchmark = True
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# enable_extension("omni.kit.window.viewport")  # enable legacy viewport interface
import omni.replicator.core as rep
from omniisaacgymenvs.tasks.base.rl_task import RLTask
from omniisaacgymenvs.robots.articulations.views.ur5e_view import UR5eView
from omniisaacgymenvs.tasks.utils.get_toolmani_assets import get_robot, get_object, get_goal
from omniisaacgymenvs.tasks.utils.pcd_processing import get_pcd, pcd_registration
from omniisaacgymenvs.tasks.utils.pcd_writer import PointcloudWriter
from omniisaacgymenvs.tasks.utils.pcd_listener import PointcloudListener

from omni.isaac.core.prims import RigidPrimView
from omni.isaac.core.utils.types import ArticulationActions

from skrl.utils import omniverse_isaacgym_utils

from pytorch3d.transforms import quaternion_to_matrix,axis_angle_to_quaternion, quaternion_multiply

# post_physics_step calls
# - get_observations()
# - get_states()
# - calculate_metrics()
# - is_done()
# - get_extras()    


class PCDMovingObjectTaskMulti(RLTask):
    def __init__(self, name, sim_config, env, offset=None) -> None:
        #################### BSH
        self.rep = rep
        self.camera_width = 640
        self.camera_height = 640
        #################### BSH

        self.update_config(sim_config)

        self.step_num = 0
        self.dt = 1 / 120.0
        self._env = env

        self.robot_list = ['ur5e_tool', 'ur5e_fork', 'ur5e_knife', 'ur5e_ladle', 'ur5e_spatular', 'ur5e_spoon']
        # self.robot_list = ['ur5e_tool']
        self.robot_num = len(self.robot_list)
        self.initial_object_goal_distance = torch.empty(self._num_envs*len(self.robot_list)).to(self.cfg["rl_device"])
        self.completion_reward = torch.zeros(self._num_envs*len(self.robot_list)).to(self.cfg["rl_device"])
        
        self.relu = torch.nn.ReLU()

        # tool orientation
        self.tool_rot_x = 70 # 70 degree
        self.tool_rot_z = 0     # 0 degree
        self.tool_rot_y = -90 # -90 degree

        # workspace 2D boundary
        self.x_min = 0.3
        self.x_max = 0.9
        self.y_min = -0.7
        self.y_max = 0.7
        self.z_min = 0.1
        self.z_max = 0.7
        
        self._pcd_sampling_num = self._task_cfg["sim"]["point_cloud_samples"]
        # observation and action space
        pcd_observations = self._pcd_sampling_num * 2 * 3   # TODO: 환경 개수 * 로봇 대수 인데 이게 맞는지 확인 필요
        # 2 is a number of point cloud masks(tool and object) and 3 is a cartesian coordinate
        self._num_observations = pcd_observations + 6 + 6 + 3 + 4 + 2
        '''
        refer to observations in get_observations()
        pcd_observations                              # [NE, 3*2*pcd_sampling_num]
        dof_pos_scaled,                               # [NE, 6]
        dof_vel_scaled[:, :6] * generalization_noise, # [NE, 6]
        flange_pos,                                   # [NE, 3]
        flange_rot,                                   # [NE, 4]
        goal_pos_xy,                                  # [NE, 2]
        
        '''

        self.exp_dict = {}
        # get tool and object point cloud
        for name in self.robot_list:
            # get tool pcd
            tool_name = name.split('_')[1]
            tool_ply_path = f"/home/bak/.local/share/ov/pkg/isaac_sim-2023.1.1/OmniIsaacGymEnvs/omniisaacgymenvs/robots/articulations/ur5e_tool/usd/tool/{tool_name}/{tool_name}.ply"
            tool_pcd = get_pcd(tool_ply_path, self._num_envs, self._pcd_sampling_num, device=self.cfg["rl_device"], tools=True)

            # get object pcd
            object_ply_path = f"/home/bak/.local/share/ov/pkg/isaac_sim-2023.1.1/OmniIsaacGymEnvs/omniisaacgymenvs/robots/articulations/ur5e_tool/usd/cylinder/cylinder.ply"
            object_pcd = get_pcd(object_ply_path, self._num_envs, self._pcd_sampling_num, device=self.cfg["rl_device"], tools=False)

            self.exp_dict[name] = {
                'tool_pcd' : tool_pcd,
                'object_pcd' : object_pcd,
            }

        if self._control_space == "joint":
            self._num_actions = 6
        elif self._control_space == "cartesian":
            self._num_actions = 7   # 3 for position, 4 for rotation(quaternion)
        else:
            raise ValueError("Invalid control space: {}".format(self._control_space))

        self._flange_link = "tool0"
        
        self.PointcloudWriter = PointcloudWriter
        self.PointcloudListener = PointcloudListener

        # Solving I.K. with cuRobo
        self.init_cuRobo()
        

        RLTask.__init__(self, name, env)


    def init_cuRobo(self):
        # Solving I.K. with cuRobo
        tensor_args = TensorDeviceType()
        robot_config_file = load_yaml(join_path(get_robot_configs_path(), "ur5e.yml"))
        robot_config = robot_config_file["robot_cfg"]
        collision_file = "/home/bak/.local/share/ov/pkg/isaac_sim-2023.1.1/OmniIsaacGymEnvs/omniisaacgymenvs/robots/articulations/ur5e_tool/collision_bar.yml"
        
        world_cfg = WorldConfig.from_dict(load_yaml(collision_file))

        ik_config = IKSolverConfig.load_from_robot_config(
            robot_config,
            world_cfg,
            rotation_threshold=0.05,
            position_threshold=0.005,
            num_seeds=20,
            self_collision_check=True,
            self_collision_opt=True,
            tensor_args=tensor_args,
            use_cuda_graph=True,
            ee_link_name="tool0",
        )
        self.ik_solver = IKSolver(ik_config)


    def update_config(self, sim_config):
        self._sim_config = sim_config
        self._cfg = sim_config.config
        self._task_cfg = sim_config.task_config

        self._num_envs = self._task_cfg["env"]["numEnvs"]
        self._env_spacing = self._task_cfg["env"]["envSpacing"]
        self._sub_spacing = self._task_cfg["env"]["subSpacing"]

        self._max_episode_length = self._task_cfg["env"]["episodeLength"]

        self._action_scale = self._task_cfg["env"]["actionScale"]
        # self.start_position_noise = self._task_cfg["env"]["startPositionNoise"]
        # self.start_rotation_noise = self._task_cfg["env"]["startRotationNoise"]

        self._dof_vel_scale = self._task_cfg["env"]["dofVelocityScale"]

        self._control_space = self._task_cfg["env"]["controlSpace"]
        self._goal_mark = self._task_cfg["env"]["goal"]
        self._object_position = self._task_cfg["env"]["object"]
        self._pcd_normalization = self._task_cfg["sim"]["point_cloud_normalization"]


    def set_up_scene(self, scene) -> None:
        for idx, name in enumerate(self.robot_list):
            # Make the suv-environments into a grid
            x = idx // 2
            y = idx % 2
            get_robot(name, self._sim_config, self.default_zero_env_path,
                      translation=torch.tensor([x * self._sub_spacing, y * self._sub_spacing, 0.0]))
            get_object(name+'_object', self._sim_config, self.default_zero_env_path)
            get_goal(name+'_goal',self._sim_config, self.default_zero_env_path)
        self.robot_num = len(self.robot_list)

        super().set_up_scene(scene)

        for idx, name in enumerate(self.robot_list):
            self.exp_dict[name]['robot_view'] = UR5eView(prim_paths_expr=f"/World/envs/.*/{name}", name=f"{name}_view")
            self.exp_dict[name]['object_view'] = RigidPrimView(prim_paths_expr=f"/World/envs/.*/{name}_object", name=f"{name}_object_view", reset_xform_properties=False)
            self.exp_dict[name]['goal_view'] = RigidPrimView(prim_paths_expr=f"/World/envs/.*/{name}_goal", name=f"{name}_goal_view", reset_xform_properties=False)
            
            # offset is only need for the object and goal
            x = idx // 2
            y = idx % 2
            self.exp_dict[name]['offset'] = torch.tensor([x * self._sub_spacing,
                                                          y* self._sub_spacing,
                                                          0.0],
                                                          device=self._device).repeat(self.num_envs, 1)

            scene.add(self.exp_dict[name]['robot_view'])
            scene.add(self.exp_dict[name]['robot_view']._flanges)
            scene.add(self.exp_dict[name]['robot_view']._tools)

            scene.add(self.exp_dict[name]['object_view'])
            scene.add(self.exp_dict[name]['goal_view'])
        self.ref_robot = self.exp_dict[name]['robot_view']

        self.init_data()
        

    def init_data(self) -> None:
        self.robot_default_dof_pos = torch.tensor(np.radians([-60, -80, 80, -90, -90, -40,
                                                              0, 30, 0.0, 0, -0.03]), device=self._device, dtype=torch.float32)
        ''' ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint', 'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint',
             'grasped_position', 'flange_revolute_yaw', 'flange_revolute_pitch', 'flange_revolute_roll', 'tool_prismatic'] '''
        self.actions = torch.zeros((self._num_envs*self.robot_num, self.num_actions), device=self._device)

        self.jacobians = torch.zeros((self._num_envs*self.robot_num, 15, 6, 11), device=self._device)
        ''' ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint', 'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint',
             'grasped_position', 'flange_revolute_yaw', 'flange_revolute_pitch', 'flange_revolute_roll', 'tool_prismatic'] '''
        self.flange_pos = torch.zeros((self._num_envs*self.robot_num, 3), device=self._device)
        self.flange_rot = torch.zeros((self._num_envs*self.robot_num, 4), device=self._device)

        self.empty_separated_envs = [torch.empty(0, dtype=torch.int32, device=self._device) for _ in self.robot_list]
        self.total_env_ids = torch.arange(self._num_envs*self.robot_num, dtype=torch.int32, device=self._device)
        self.local_env_ids = torch.arange(self.ref_robot.count, dtype=torch.int32, device=self._device)

    # change from RLTask.cleanup()
    def cleanup(self) -> None:
        """Prepares torch buffers for RL data collection."""

        # prepare tensors
        self.obs_buf = torch.zeros((self._num_envs*self.robot_num, self.num_observations), device=self._device, dtype=torch.float)
        self.states_buf = torch.zeros((self._num_envs*self.robot_num, self.num_states), device=self._device, dtype=torch.float)
        self.rew_buf = torch.zeros(self._num_envs*self.robot_num, device=self._device, dtype=torch.float)
        self.reset_buf = torch.ones(self._num_envs*self.robot_num, device=self._device, dtype=torch.long)
        self.progress_buf = torch.zeros(self._num_envs*self.robot_num, device=self._device, dtype=torch.long)
        self.extras = {}


    def post_reset(self):
        self.num_robot_dofs = self.exp_dict['ur5e_fork']['robot_view'].num_dof
        
        dof_limits = self.exp_dict['ur5e_fork']['robot_view'].get_dof_limits()  # every robot has the same dof limits
        # dof_limits = dof_limits.repeat(self.robot_num, 1, 1)

        self.robot_dof_lower_limits = dof_limits[0, :, 0].to(device=self._device)
        self.robot_dof_upper_limits = dof_limits[0, :, 1].to(device=self._device)
        self.robot_dof_speed_scales = torch.ones_like(self.robot_dof_lower_limits)
        # self.robot_dof_targets = torch.zeros((self._num_envs*self.robot_num, self.num_robot_dofs), dtype=torch.float, device=self._device)
        self.robot_dof_targets = self.robot_default_dof_pos.unsqueeze(0).repeat(self.num_envs*self.robot_num, 1)
        self.zero_joint_velocities = torch.zeros((self._num_envs*self.robot_num, self.num_robot_dofs), dtype=torch.float, device=self._device)
        
        for name in self.robot_list:
            self.exp_dict[name]['object_view'].enable_rigid_body_physics()
            self.exp_dict[name]['object_view'].enable_gravities()

        indices = torch.arange(self._num_envs*self.robot_num, dtype=torch.int64, device=self._device)
        self.reset_idx(indices)

        for idx, name in enumerate(self.robot_list):
            self.exp_dict[name]['object_view'].enable_rigid_body_physics()
            self.exp_dict[name]['object_view'].enable_gravities()
            self.exp_dict[name]['goal_view'].disable_rigid_body_physics()


    def pre_physics_step(self, actions) -> None:
        reset_env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        if len(reset_env_ids) > 0:
            self.reset_idx(reset_env_ids)

        if self.step_num == 0:
            pass    # At the very first step, action dimension is [num_envs, 7]. but it should be [num_envs*robot_num, 7]
        else:
            self.actions = actions.clone().to(self._device)
        env_ids_int32 = torch.arange(self.ref_robot.count,
                                     dtype=torch.int32,
                                     device=self._device)

        if self._control_space == "joint":
            dof_targets = self.robot_dof_targets[:, :6] + self.robot_dof_speed_scales[:6] * self.dt * self.actions * self._action_scale

        elif self._control_space == "cartesian":
            goal_position = self.flange_pos + self.actions[:, :3] / 70.0
            goal_orientation = self.flange_rot + self.actions[:, 3:] / 70.0
            flange_link_idx = self.ref_robot.body_names.index(self._flange_link)
            delta_dof_pos = omniverse_isaacgym_utils.ik(
                                                        jacobian_end_effector=self.jacobians[:, flange_link_idx-1, :, :6],
                                                        current_position=self.flange_pos,
                                                        current_orientation=self.flange_rot,
                                                        goal_position=goal_position,
                                                        goal_orientation=goal_orientation
                                                        )
            '''jacobian : (self._num_envs, num_of_bodies-1, wrench, num of joints)
            num_of_bodies - 1 due to start from 0 index
            '''
            dof_targets = self.robot_dof_targets[:, :6] + delta_dof_pos[:, :6]

        self.robot_dof_targets[:, :6] = torch.clamp(dof_targets, self.robot_dof_lower_limits[:6], self.robot_dof_upper_limits[:6])

        self.robot_dof_targets[:, 7] = torch.deg2rad(torch.tensor(self.tool_rot_x, device=self._device))
        self.robot_dof_targets[:, 8] = torch.deg2rad(torch.tensor(self.tool_rot_z, device=self._device))
        self.robot_dof_targets[:, 9] = torch.deg2rad(torch.tensor(self.tool_rot_y, device=self._device))
        ### TODO: 나중에는 윗 줄을 통해 tool position이 random position으로 고정되도록 변수화. reset_idx도 확인할 것

        indices = torch.tensor([0, 6, 12], dtype=torch.int32, device=self._device)

        for name in self.robot_list:
            '''
            Caution:
             DO NOT USE set_joint_positions at pre_physics_step !!!!!!!!!!!!!!
             set_joint_positions: This method will immediately set (teleport) the affected joints to the indicated value.
                                  It make the robot unstable.
             set_joint_position_targets: Set the joint position targets for the implicit Proportional-Derivative (PD) controllers
             apply_action: apply multiple targets (position, velocity, and/or effort) in the same call
             (effort control means force/torque control)
            '''
            robot_env_ids = self.local_env_ids * self.robot_num + self.robot_list.index(name)
            robot_dof_targets = self.robot_dof_targets[robot_env_ids]
            articulation_actions = ArticulationActions(joint_positions=robot_dof_targets)
            self.exp_dict[name]['robot_view'].apply_action(articulation_actions, indices=self.local_env_ids)
            # apply_action의 환경 indices에 env_ids외의 index를 넣으면 GPU오류가 발생한다. 그래서 env_ids를 넣어야 한다.


    def separate_env_ids(self, env_ids):
        # Calculate the local index for each env_id
        local_indices = env_ids // self.robot_num
        # Calculate indices for each env_id based on its original value
        robot_indices = env_ids % self.robot_num
        # Create a mask for each robot
        masks = torch.stack([robot_indices == i for i in range(self.robot_num)], dim=1)
        # Apply the masks to separate the env_ids
        separated_envs = [local_indices[mask] for mask in masks.unbind(dim=1)]
        
        return separated_envs
    

    def reset_idx(self, env_ids) -> None:
        """
        Parameters  
        ----------
        exp_done_info : dict
        {
            "ur5e_spoon": torch.tensor([0 , 2, 3], device='cuda:0', dtype=torch.int32),
            "ur5e_spatular": torch.tensor([1, 2, 3], device='cuda:0', dtype=torch.int32),
            "ur5e_ladle": torch.tensor([3], device='cuda:0', dtype=torch.int32),
            "ur5e_fork": torch.tensor([2, 3], device='cuda:0', dtype=torch.int32),
            ...
        }
        """
        env_ids = env_ids.to(dtype=torch.int32)

        # Split env_ids using the split_env_ids function
        separated_envs = self.separate_env_ids(env_ids)

        for idx, sub_envs in enumerate(separated_envs):
            if sub_envs.size(0) == 0:
                continue

            robot_name = self.robot_list[idx]
            robot_dof_targets = self.robot_dof_targets[sub_envs, :]

            # reset object
            object_position = torch.tensor(self._object_position, device=self._device)  # objects' local pos
            object_pos = object_position.repeat(len(sub_envs), 1)
            orientation = torch.tensor([1.0, 0.0, 0.0, 0.0], device=self._device)   # objects' local orientation
            object_ori = orientation.repeat(len(sub_envs),1)
            obj_world_pos = object_pos + self.exp_dict[robot_name]['offset'][sub_envs, :] + self._env_pos[sub_envs, :]  # objects' world pos
            self.exp_dict[robot_name]['object_view'].set_world_poses(obj_world_pos,
                                                                     object_ori,
                                                                     indices=sub_envs)

            # reset goal
            goal_mark_pos = torch.tensor(self._goal_mark, device=self._device)  # goals' local pos
            goal_mark_pos = goal_mark_pos.repeat(len(sub_envs),1)
            goal_mark_ori = orientation.repeat(len(sub_envs),1)
            goals_world_pos = goal_mark_pos + self.exp_dict[robot_name]['offset'][sub_envs, :] + self._env_pos[sub_envs, :]
            self.exp_dict[robot_name]['goal_view'].set_world_poses(goals_world_pos,
                                                                   goal_mark_ori,
                                                                   indices=sub_envs)
            # self.exp_dict[robot_name]['goal_view'].disable_rigid_body_physics()

            flange_pos = deepcopy(object_pos)
            flange_pos[:, 0] -= 0.2     # x
            flange_pos[:, 1] -= 0.3     # y
            flange_pos[:, 2] = 0.4      # z

            # Extract the x and y coordinates
            flange_xy = flange_pos[:, :2]
            object_xy = object_pos[:, :2]

            direction = object_xy - flange_xy                       # Calculate the vector from flange to object
            angle = torch.atan2(direction[:, 1], direction[:, 0])   # Calculate the angle of the vector relative to the x-axis
            axis_angle_z = torch.zeros_like(flange_pos)             # Create axis-angle representation for rotation about z-axis
            axis_angle_z[:, 2] = -angle                             # Negative angle to point towards the object
            quat_z = axis_angle_to_quaternion(axis_angle_z)         # Convert to quaternion

            # Create axis-angle representation for 180-degree rotation about y-axis
            axis_angle_y = torch.tensor([0., torch.pi, 0.], device=flange_pos.device).expand_as(flange_pos)

            quat_y = axis_angle_to_quaternion(axis_angle_y)     # Convert to quaternion
            flange_ori = quaternion_multiply(quat_y, quat_z)    # Combine the rotations (first rotate around z, then around y)

            initialized_pos = Pose(flange_pos, flange_ori, name="tool0")
            target_dof_pos = torch.empty(0).to(device=self._device)

            for i in range(initialized_pos.batch):  # solve IK with cuRobo
                result = self.ik_solver.solve_single(initialized_pos[i])
                if result.success:
                    target_dof_pos = torch.cat((target_dof_pos, result.solution[result.success]), dim=0)
                else:
                    print(f"IK solver failed. Initialize a robot in {robot_name} env {sub_envs[i]} with default pose.")
                    target_dof_pos = torch.cat((target_dof_pos, self.robot_default_dof_pos[:6].unsqueeze(0)), dim=0)
            
            robot_dof_targets[:, :6] = torch.clamp(target_dof_pos,
                                                   self.robot_dof_lower_limits[:6].repeat(len(sub_envs),1),
                                                   self.robot_dof_upper_limits[:6].repeat(len(sub_envs),1))
            self.exp_dict[robot_name]['robot_view'].set_joint_positions(robot_dof_targets, indices=sub_envs)
            self.exp_dict[robot_name]['robot_view'].set_joint_position_targets(robot_dof_targets, indices=sub_envs)
            self.exp_dict[robot_name]['robot_view'].set_joint_velocities(torch.zeros((len(sub_envs), self.num_robot_dofs), device=self._device),
                                                                   indices=sub_envs)
            
            # bookkeeping
            separated_abs_env = separated_envs[idx]*self.robot_num + idx
            self.reset_buf[separated_abs_env] = 0
            self.progress_buf[separated_abs_env] = 0


    def get_observations(self) -> dict:
        self._env.render()  # add for get point cloud on headless mode
        self.step_num += 1
        ''' retrieve point cloud data from all render products '''
        # tasks/utils/pcd_writer.py 에서 pcd sample하고 tensor로 변환해서 가져옴
        # pointcloud = self.pointcloud_listener.get_pointcloud_data()

        tools_pcd_flattened = torch.empty(0).to(device=self._device)
        objects_pcd_flattened = torch.empty(0).to(device=self._device)
        object_pcd_concat = torch.empty(0).to(device=self._device)  # concatenate object point cloud for getting xyz position
        self.goal_pos = torch.empty(self._num_envs*self.robot_num, 3).to(device=self._device)  # save goal position for getting xy position
        robots_dof_pos = torch.empty(self._num_envs*self.robot_num, 6).to(device=self._device)
        robots_dof_vel = torch.empty(self._num_envs*self.robot_num, 6).to(device=self._device)

        for idx, robot_name in enumerate(self.robot_list):
            local_abs_env_ids = self.local_env_ids*self.robot_num + idx
            robot_flanges = self.exp_dict[robot_name]['robot_view']._flanges
            self.flange_pos[local_abs_env_ids], self.flange_rot[local_abs_env_ids] = robot_flanges.get_local_poses()
            object_pos, object_rot_quaternion = self.exp_dict[robot_name]['object_view'].get_local_poses()
            
            # local object pose values are indicate with the environment ids with regard to the robot set
            x = (idx // 2) * self._sub_spacing
            y = (idx % 2) * self._sub_spacing
            object_pos[:, :2] -= torch.tensor([x, y], device=self._device)
            
            object_rot = quaternion_to_matrix(object_rot_quaternion)
            tool_pos, tool_rot_quaternion = self.exp_dict[robot_name]['robot_view']._tools.get_local_poses()
            tool_rot = quaternion_to_matrix(tool_rot_quaternion)

            # concat tool point cloud
            tool_pcd_transformed = pcd_registration(self.exp_dict[robot_name]['tool_pcd'],
                                                    tool_pos,
                                                    tool_rot,
                                                    self.num_envs,
                                                    device=self._device)
            tool_pcd_flattend = tool_pcd_transformed.contiguous().view(self._num_envs, -1)
            tools_pcd_flattened = torch.cat((tools_pcd_flattened, tool_pcd_flattend), dim=0)

            # concat object point cloud
            object_pcd_transformed = pcd_registration(self.exp_dict[robot_name]['object_pcd'],
                                                      object_pos,
                                                      object_rot,
                                                      self.num_envs,
                                                      device=self._device)
            object_pcd_concat = torch.cat((object_pcd_concat, object_pcd_transformed), dim=0)   # concat for calculating xyz position
            object_pcd_flattend = object_pcd_transformed.contiguous().view(self._num_envs, -1)
            objects_pcd_flattened = torch.cat((objects_pcd_flattened, object_pcd_flattend), dim=0)

            self.goal_pos[local_abs_env_ids] = self.exp_dict[robot_name]['goal_view'].get_local_poses()[0]

            # get robot dof position and velocity from 1st to 6th joint
            robots_dof_pos[local_abs_env_ids] = self.exp_dict[robot_name]['robot_view'].get_joint_positions(clone=False)[:, 0:6]
            robots_dof_vel[local_abs_env_ids] = self.exp_dict[robot_name]['robot_view'].get_joint_velocities(clone=False)[:, 0:6]
            # rest of the joints are not used for control. They are fixed joints at each episode.
        
        self.object_pos_xyz = torch.mean(object_pcd_concat, dim=1)
        self.object_pos_xy = self.object_pos_xyz[:, [0, 1]]

        self.goal_pos_xy = self.goal_pos[:, [0, 1]]

        '''self.object_pos_xy는 [num_envs * robot_num, 2]의 형태로 되어야 함'''

        # # normalize robot_dof_pos
        # dof_pos_scaled = 2.0 * (robots_dof_pos - self.robot_dof_lower_limits) \
        #     / (self.robot_dof_upper_limits - self.robot_dof_lower_limits) - 1.0   # normalized by [-1, 1]
        # dof_pos_scaled = (robots_dof_pos - self.robot_dof_lower_limits) \
        #                 /(self.robot_dof_upper_limits - self.robot_dof_lower_limits)    # normalized by [0, 1]
        dof_pos_scaled = robots_dof_pos    # non-normalized

        # # normalize robot_dof_vel
        # dof_vel_scaled = robots_dof_vel * self._dof_vel_scale
        # generalization_noise = torch.rand((dof_vel_scaled.shape[0], 6), device=self._device) + 0.5
        dof_vel_scaled = robots_dof_vel    # non-normalized

        '''
        아래 순서로 최종 obs_buf에 concat. 첫 차원은 환경 갯수
        1. tool point cloud (flattened)
        2. object object point cloud (flattened)
        3. robot dof position
        4. robot dof velocity
        5. flange position
        6. flange orientation
        7. goal position
        '''

        '''NE = self._num_envs * self.robot_num'''
        self.obs_buf = torch.cat((
                                #   tool_pcd_transformed.reshape([tool_pcd_transformed.shape[0], -1]),     # [NE, N*3], point cloud
                                #   object_pcd_transformed.reshape([object_pcd_transformed.shape[0], -1]), # [NE, N*3], point cloud
                                #   tool_pcd_transformed.contiguous().view(self._num_envs, -1),   # [NE, N*3], point cloud
                                #   object_pcd_transformed.contiguous().view(self._num_envs, -1), # [NE, N*3], point cloud
                                  tools_pcd_flattened,                                          # [NE, N*3], point cloud
                                  objects_pcd_flattened,                                        # [NE, N*3], point cloud
                                  dof_pos_scaled,                                               # [NE, 6]
                                #   dof_vel_scaled[:, :6] * generalization_noise, # [NE, 6]
                                  dof_vel_scaled,                                               # [NE, 6]
                                  self.flange_pos,                                              # [NE, 3]
                                  self.flange_rot,                                              # [NE, 4]
                                  self.goal_pos_xy,                                             # [NE, 2]
                                 ), dim=1)

        if self._control_space == "cartesian":
            ''' 위에있는 jacobian 차원을 참고해서 값을 넣어주어야 함. 로봇 종류에 맞추어 넣어주어야 할 것 같다.
             '''
            for idx, name in enumerate(self.robot_list):
                self.jacobians[idx*self.num_envs:(idx+1)*self.num_envs] = self.exp_dict[name]['robot_view'].get_jacobians(clone=False)

        # TODO: name???? 
        # return {self.exp_dict['ur5e_fork']['robot_view'].name: {"obs_buf": self.obs_buf}}
        return self.obs_buf


    def calculate_metrics(self) -> None:
        initialized_idx = self.progress_buf == 1    # initialized index를 통해 progress_buf가 1인 경우에만 initial distance 계산
        self.completion_reward[:] = 0.0 # reset completion reward
        current_object_goal_distance = LA.norm(self.goal_pos_xy - self.object_pos_xy, ord=2, dim=1)
        self.initial_object_goal_distance[initialized_idx] = current_object_goal_distance[initialized_idx]

        init_o_g_d = self.initial_object_goal_distance
        cur_o_g_d = current_object_goal_distance
        object_goal_distance_reward = self.relu(-(cur_o_g_d - init_o_g_d)/init_o_g_d)

        # completion reward
        self.done_envs = cur_o_g_d <= 0.1
        # completion_reward = torch.where(self.done_envs, torch.full_like(cur_t_g_d, 100.0)[self.done_envs], torch.zeros_like(cur_t_g_d))
        self.completion_reward[self.done_envs] = torch.full_like(cur_o_g_d, 300.0)[self.done_envs]

        total_reward = object_goal_distance_reward + self.completion_reward

        self.rew_buf[:] = total_reward
    

    def is_done(self) -> None:
        ones = torch.ones_like(self.reset_buf)
        reset = torch.zeros_like(self.reset_buf)

        # # workspace regularization
        reset = torch.where(self.flange_pos[:, 0] < self.x_min, ones, reset)
        reset = torch.where(self.flange_pos[:, 1] < self.y_min, ones, reset)
        reset = torch.where(self.flange_pos[:, 0] > self.x_max, ones, reset)
        reset = torch.where(self.flange_pos[:, 1] > self.y_max, ones, reset)
        reset = torch.where(self.flange_pos[:, 2] > 0.5, ones, reset)
        reset = torch.where(self.object_pos_xy[:, 0] < self.x_min, ones, reset)
        reset = torch.where(self.object_pos_xy[:, 1] < self.y_min, ones, reset)
        reset = torch.where(self.object_pos_xy[:, 0] > self.x_max, ones, reset)
        reset = torch.where(self.object_pos_xy[:, 1] > self.y_max, ones, reset)
        # reset = torch.where(self.object_pos_xy[:, 2] > 0.5, ones, reset)

        # object reached
        reset = torch.where(self.done_envs, ones, reset)

        # max episode length
        self.reset_buf = torch.where(self.progress_buf >= self._max_episode_length - 1, ones, reset)




    # def visualize_point_cloud(self, view_idx, lidar_position):
    #     '''
    #     args:
    #         view_idx: index of the cloner
    #         lidar_position: position of the lidar
    #     '''
    #     flange_pos, flange_rot = self._flanges.get_local_poses()

    #     lidar_prim_path = self._point_cloud[view_idx].prim_path
    #     point_cloud = self._point_cloud[view_idx]._lidar_sensor_interface.get_point_cloud_data(lidar_prim_path)
    #     semantic = self._point_cloud[view_idx]._lidar_sensor_interface.get_semantic_data(lidar_prim_path)

    #     pcl_reshape = np.reshape(point_cloud, (point_cloud.shape[0]*point_cloud.shape[1], 3))
    #     flange_pos_np = flange_pos[view_idx].cpu().numpy()
    #     flange_ori_np = flange_rot[view_idx].cpu().numpy()

    #     pcl_semantic = np.reshape(semantic, -1)        


    #     v3d = o3d.utility.Vector3dVector

    #     # get point cloud
    #     pcd = o3d.geometry.PointCloud()
    #     pcd.points = v3d(pcl_reshape)

    #     # get sampled point cloud
    #     idx = pcu.downsample_point_cloud_poisson_disk(pcl_reshape, num_samples=int(0.2*pcl_reshape.shape[0]))
    #     pcl_reshape_sampled = pcl_reshape[idx]
    #     sampled_pcd = o3d.geometry.PointCloud()
    #     sampled_pcd.points = v3d(pcl_reshape_sampled)

    #     # get lidar frame. lidar frame is a world [0, 0, 0] frame
    #     lidar_coord = o3d.geometry.TriangleMesh().create_coordinate_frame(size=0.2, origin=np.array([0.0, 0.0, 0.0]))

    #     # get base pose    
    #     T_b = np.eye(4)
    #     # TODO: get rotation matrix from USD
    #     # R_b = lidar_coord.get_rotation_matrix_from_xyz((np.pi/6, 0, -np.pi/2))
    #     R_b = lidar_coord.get_rotation_matrix_from_xyz((np.pi/9, 0, -np.pi/2))  # rotation relationship between lidar and base
    #     T_b[:3, :3] = R_b
    #     T_b[:3, 3] = lidar_position # acquired position from prim_path is [0,0,0]
    #     T_b_inv = np.linalg.inv(T_b)
    #     base_coord = copy.deepcopy(lidar_coord).transform(T_b_inv)
    #     # o3d.visualization.draw_geometries([pcd, lidar_coord, base_coord])

    #     # get ee pose
    #     T_ee = np.eye(4)
    #     R_ee = base_coord.get_rotation_matrix_from_quaternion(flange_ori_np)
    #     T_ee[:3, :3] = R_ee
    #     T_ee[:3, 3] = flange_pos_np
    #     T_l_e = np.matmul(T_b_inv, T_ee)
    #     flange_coord = copy.deepcopy(lidar_coord).transform(T_l_e)
    #     # o3d.visualization.draw_geometries([pcd, lidar_coord, base_coord, flange_coord],
    #     #                                   window_name=f'scene of env_{view_idx}')


    #     # print(f'env index: {view_idx}', np.unique(pcl_semantic))

    #     # 아래는 마지막 index만 가시화 (도구 가시화를 의도함)
    #     # index = np.unique(pcl_semantic)[-1]
    #     # print(f'show index: {index}\n')
    #     # semantic = np.where(pcl_semantic==index)[0]
    #     # pcd_semantic = o3d.geometry.PointCloud()
    #     # pcd_semantic.points = o3d.utility.Vector3dVector(pcl_reshape[semantic])
    #     # o3d.visualization.draw_geometries([pcd_semantic, lidar_coord, base_coord, flange_coord],
    #     #                                     window_name=f'semantic_{index} of env_{view_idx}')
        

    #     for i in np.unique(pcl_semantic):
    #         semantic = np.where(pcl_semantic==i)[0]
    #         pcd_semantic = o3d.geometry.PointCloud()
    #         pcd_semantic.points = o3d.utility.Vector3dVector(pcl_reshape[semantic])
    #         o3d.visualization.draw_geometries([pcd_semantic, lidar_coord, base_coord, flange_coord],
    #                                           window_name=f'semantic_{i} of env_{view_idx}')

    ##################### ####################################
    ################# visualize point cloud #################
    # view_idx = 0

    # base_coord = o3d.geometry.TriangleMesh().create_coordinate_frame(size=0.15, origin=np.array([0.0, 0.0, 0.0]))
    # tool_pos_np = tool_pos[view_idx].cpu().numpy()
    # tool_rot_np = tool_rot[view_idx].cpu().numpy()
    # tgt_pos_np = object_pos[view_idx].cpu().numpy()
    # tgt_rot_np = object_rot[view_idx].cpu().numpy()
    
    # tool_transformed_pcd_np = tool_pcd_transformed[view_idx].squeeze(0).detach().cpu().numpy()
    # tool_transformed_point_cloud = o3d.geometry.PointCloud()
    # tool_transformed_point_cloud.points = o3d.utility.Vector3dVector(tool_transformed_pcd_np)
    # T_t = np.eye(4)
    # T_t[:3, :3] = tool_rot_np
    # T_t[:3, 3] = tool_pos_np
    # tool_coord = copy.deepcopy(base_coord).transform(T_t)

    # tool_end_point = o3d.geometry.TriangleMesh().create_sphere(radius=0.01)
    # tool_end_point.paint_uniform_color([0, 0, 1])
    # farthest_pt = tool_transformed_pcd_np[farthest_idx.detach().cpu().numpy()][view_idx]
    # T_t_p = np.eye(4)
    # T_t_p[:3, 3] = farthest_pt
    # tool_tip_position = copy.deepcopy(tool_end_point).transform(T_t_p)

    # tgt_transformed_pcd_np = object_pcd_transformed[view_idx].squeeze(0).detach().cpu().numpy()
    # tgt_transformed_point_cloud = o3d.geometry.PointCloud()
    # tgt_transformed_point_cloud.points = o3d.utility.Vector3dVector(tgt_transformed_pcd_np)
    # T_o = np.eye(4)

    # # R_b = tgt_rot_np.get_rotation_matrix_from_xyz((np.pi/2, 0, 0))
    # T_o[:3, :3] = tgt_rot_np
    # # T_o[:3, :3] = R_b
    # T_o[:3, 3] = tgt_pos_np
    # tgt_coord = copy.deepcopy(base_coord).transform(T_o)

    # self.goal_pos, _ = self._goals.get_local_poses()
    # goal_pos_np = self.goal_pos[view_idx].cpu().numpy()
    # goal_cone = o3d.geometry.TriangleMesh.create_cone(radius=0.01, height=0.03)
    # goal_cone.paint_uniform_color([0, 1, 0])
    # T_g_p = np.eye(4)
    # T_g_p[:3, 3] = goal_pos_np
    # goal_position = copy.deepcopy(goal_cone).transform(T_g_p)

    # goal_pos_xy_np = copy.deepcopy(goal_pos_np)
    # goal_pos_xy_np[2] = self.object_height
    # goal_sphere = o3d.geometry.TriangleMesh.create_sphere(radius=0.01)
    # goal_sphere.paint_uniform_color([1, 0, 0])
    # T_g = np.eye(4)
    # T_g[:3, 3] = goal_pos_xy_np
    # goal_position_xy = copy.deepcopy(goal_sphere).transform(T_g)

    # o3d.visualization.draw_geometries([base_coord,
    #                                    tool_transformed_point_cloud,
    #                                    tgt_transformed_point_cloud,
    #                                    tool_tip_position,
    #                                    tool_coord,
    #                                    tgt_coord,
    #                                    goal_position,
    #                                    goal_position_xy],
    #                                     window_name=f'point cloud')
    ################# visualize point cloud #################
    #########################################################