from abc import ABC
import gym
import numpy as np
from .detpmp_model import DeterministicProMP
import torch as th
from .controller import PosController, PDController, VelController, PIDController
from stable_baselines3.common.noise import NormalActionNoise, OrnsteinUhlenbeckActionNoise


class DetPMPWrapper(ABC):
    """
    This class is the wrapper for Probabilistic Movement Primitives model.

    :param env: for any regular OpenAI Gym environment.
    :param num_dof: the degree of freedom of the robot
    :param num_basis: the number of Gaussian Basis Functions.
    :param width: the width of Gaussian Basis Functions.
    :param step_length: the episode length.
    :param weights_scale: the scale of the weights
    :param zero_start: whether start from the initial position or not
    :param zero_basis: the basis functions when zero_start is True.
    :param mp_kwargs: the parameter of the controller
    """

    def __init__(
            self,
            env: gym.Wrapper,
            num_dof: int,
            num_basis: int,
            width: int,
            step_length=None,
            weights_scale=1,
            zero_start=False,
            zero_basis=0,
            noise_sigma=0,
            **controller_kwargs):

        self.controller_type = controller_kwargs['controller_type']
        self.controller_setup(env=env, controller_kwargs=controller_kwargs, num_dof=num_dof)

        self.zero_start = zero_start
        self.start_traj = None
        self.trajectory = None
        self.velocity = None
        self.noise = NormalActionNoise(mean=np.zeros(num_dof), sigma=noise_sigma * np.ones(num_dof))

        self.step_length = step_length
        self.env = env
        dt = self.env.dt

        self.num_dof = num_dof
        self.mp = DeterministicProMP(n_basis=num_basis, n_dof=num_dof, width=width,
                                     zero_start=zero_start, n_zero_bases=zero_basis,
                                     step_length=self.step_length, dt=dt, weight_scale=weights_scale)


    def controller_setup(self, env, controller_kwargs, num_dof):
        """
        This function builds up the controller of ProMP.
        """
        if self.controller_type == 'motor':
            self.controller = PDController(env, p_gains=controller_kwargs['controller_kwargs']['p_gains'],
                                           d_gains=controller_kwargs['controller_kwargs']['d_gains'], num_dof=num_dof)
        elif self.controller_type == 'position':
            self.controller = PosController(env, num_dof=num_dof)
        elif self.controller_type == 'velocity':
            self.controller = VelController(env, num_dof=num_dof)
        elif self.controller_type == 'pid':
            self.controller = PIDController(env, p_gains=controller_kwargs['controller_kwargs']['p_gains'],
                                            i_gains=controller_kwargs['controller_kwargs']['i_gains'],
                                            d_gains=controller_kwargs['controller_kwargs']['d_gains'],
                                            num_dof=num_dof)
        else:
            raise AssertionError("controller not exist")

    def update(self):
        """
        This function build up the reference trajectory of ProMP
        according to the current weights in each iteration.
        """
        # torch version of the reference trajectory
        _,  self.trajectory, self.velocity, self.acceleration = self.mp.compute_trajectory()

        # add initial position
        if self.zero_start:
            if self.controller_type == 'motor':
                self.trajectory += th.Tensor(
                    self.controller.obs()[-2 * self.num_dof:-1 * self.num_dof].reshape(self.num_dof)).to(device='cuda')
                #self.velocity += th.Tensor(
                #    self.controller.obs()[-2 * self.num_dof:-1 * self.num_dof].reshape(self.num_dof)).to(device='cuda')
            elif self.controller_type == 'position':
                self.trajectory += th.Tensor(self.controller.obs()[-self.num_dof:].reshape(self.num_dof)).to(
                    device='cuda')

        # numpy version of the reference trajectory
        self.trajectory_np = self.trajectory.cpu().detach().numpy()
        self.velocity_np = self.velocity.cpu().detach().numpy()
        self.acceleration_np = self.acceleration.cpu().detach().numpy()


    def predict_action(self, step, observation):
        """
        This function predicts the actions according to the Replay Buffer observations.
        It is used for critic network and actor policy updating.

        Input:
            step: the timestep information stored in Replay Buffer.
            observation: the observation stored in Replay Buffer.
        Return:
            action: the action based on current ProMP parameters.
        """
        self.positions = self.trajectory[step].reshape(-1, self.num_dof)
        self.velocities = self.velocity[step].reshape(-1, self.num_dof)
        self.accelerations = self.acceleration[step].reshape(-1, self.num_dof)
        actions = self.controller.predict_actions(self.positions, self.velocities, self.accelerations, observation)
        return actions

    def get_action(self, timesteps, noise=0.3):
        """
        This function generates the actions according to the observation of the environment.
        It is used for interacting with the environment.

        Input:
            step: the timestep information.
        Return:
            action: the action used for indicating the movements of the robot.
        """
        noise_dist = NormalActionNoise(mean=np.zeros(self.num_dof), sigma=noise * np.ones(self.num_dof))

        trajectory = self.trajectory_np[timesteps] + noise_dist()
        velocity = self.velocity_np[timesteps] #+ self.noise()
        acceleration = self.acceleration_np[timesteps]
        action, des_pos, des_vel = self.controller.get_action(trajectory, velocity, acceleration)
        return action

    def eval_rollout(self, env):
        """
        This function evaluate the current ProMP.

        Input:
            step: the environment without normalization.
            (We don't use normalization for the environment in our implementation,
            so this environment is same as the environment we used for sampling data.)
        Return:
            episode_reward: the reward of one episode based on current ProMP model.
            step_length: the step length of one episode based on current ProMP model.
        """
        rewards = 0
        step_length = self.step_length
        env.reset()
        #env.render()

        if "Meta" in str(env):
            for i in range(int(self.step_length)):
                ac = self.get_action(i)
                ac = np.clip(ac, -1, 1).reshape(self.num_dof)
                obs, reward, dones, info = env.step(ac)
                rewards += reward
        else:
            for i in range(step_length):
                ac = self.get_action(i, noise=0)
                ac = np.clip(ac, -1, 1).reshape(1,self.num_dof)
                obs, reward, done, info = env.step(ac)
                rewards += reward
                if done:
                    step_length = i + 1
                    break


        if hasattr(self.env, "rewards_no_ip"):
            episode_reward = env.rewards_no_ip  # the total reward without initial phase
        else:
            episode_reward = rewards
        return episode_reward, step_length

    # should be deleted when finished, use render_rollout to render the environment
    '''
    def load(self, action):
        action = torch.FloatTensor(action)
        params = action.reshape(self.mp.n_basis, self.mp.n_dof) * self.weights_scale
        self.mp.weights = params.to(device="cuda")
        _, des_pos, des_vel, __ = self.mp.compute_trajectory(self.mp.weights)
        des_pos += th.Tensor(self.controller.obs()[-2*self.num_dof:-self.num_dof]).to(device='cuda')
        return des_pos, des_vel
    '''

    def render_rollout(self, weights, env):
        """
        This function render the environment.

        Input:
            weights: the learned weights.
            env: the environment we want to render.
        """
        import time
        print("render")
        self.mp.weights = th.Tensor(weights).to(device='cuda')

        self.update()

        rewards = 0
        step_length = self.step_length
        self.eval_rollout(env)
        env.reset()
        obses = []
        import time
        aces = []

        #print("init_value", self.env.sim.data.mocap_pos, self.env.sim.data.qpos, self.env.sim.data.qvel)

        if "dmc" in str(env):

            # export MUJOCO_GL="osmesa"

            for i in range(int(self.step_length)):
                time.sleep(0.1)
                ac = self.get_action(i)
                #print("ac", ac)
                ac = np.clip(ac, -1, 1).reshape(1, self.num_dof)
                #print("ac", ac)
                obs, reward, done, info = env.step(ac)
                obses.append(obs)
                aces.append(ac)
                rewards += reward
                #env.render(mode="rgb_array")
                print(i, reward)
                #env.render(mode="human")
            env.close()
        elif "Meta" in str(env):
            for i in range(int(self.step_length)):
                ac = self.get_action(i)
                ac = np.clip(ac, -1, 1).reshape(self.num_dof)
                obs, reward, dones, info = env.step(ac)
                rewards += reward
                #time.sleep(1)
                env.render(False)
        else:
            for i in range(step_length):
                #time.sleep(0.1)
                self.update()
                ac = self.get_action(i)
                print("ac", ac)
                ac = np.clip(ac, -1, 1).reshape(1, self.num_dof)

                obs, reward, done, info = env.step(ac)
                obses.append(obs)
                aces.append(ac)
                rewards += reward
                if done:
                    step_length = i + 1
                    break
                #env.render()

        print("reward", rewards)

