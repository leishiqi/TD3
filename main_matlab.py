import numpy as np
import torch
import gym
import argparse
import os

import utils
import TD3
import OurDDPG
import DDPG
import pymysql

host = 'localhost'
port = 3306
db = 'building_management'
user = 'root'
password = '971016Lsq'

parser = argparse.ArgumentParser()
parser.add_argument("--policy", default="TD3")  # Policy name (TD3, DDPG or OurDDPG)
parser.add_argument("--env", default="My_Env-v0")  # OpenAI gym environment name
parser.add_argument("--seed", default=0, type=int)  # Sets Gym, PyTorch and Numpy seeds
parser.add_argument("--start_timesteps", default=30, type=int)  # Time steps initial random policy is used
parser.add_argument("--eval_freq", default=5e3, type=int)  # How often (time steps) we evaluate
parser.add_argument("--expl_noise", default=0.1)  # Std of Gaussian exploration noise
parser.add_argument("--batch_size", default=256, type=int)  # Batch size for both actor and critic
parser.add_argument("--discount", default=0.99)  # Discount factor
parser.add_argument("--tau", default=0.005)  # Target network update rate
parser.add_argument("--policy_noise", default=0.2)  # Noise added to target policy during critic update
parser.add_argument("--noise_clip", default=0.5)  # Range to clip target policy noise
parser.add_argument("--policy_freq", default=2, type=int)  # Frequency of delayed policy updates
parser.add_argument("--save_model", action="store_true")  # Save model and optimizer parameters
parser.add_argument("--load_model", default="")  # Model load file name, "" doesn't load, "default" uses file_name
args = parser.parse_args()

file_name = f"{args.policy}_{args.env}_{args.seed}"
print("---------------------------------------")
print(f"Policy: {args.policy}, Env: {args.env}, Seed: {args.seed}")
print("---------------------------------------")

if not os.path.exists("./results"):
    os.makedirs("./results")

if args.save_model and not os.path.exists("./models"):
    os.makedirs("./models")

env = gym.make(args.env)

# Set seeds
env.seed(args.seed)
env.action_space.seed(args.seed)
torch.manual_seed(args.seed)
np.random.seed(args.seed)

state_dim = env.observation_space.shape[0]
action_dim = env.action_space.shape[0]
max_action = float(env.action_space.high[0])

kwargs = {
    "state_dim": state_dim,
    "action_dim": action_dim,
    "max_action": max_action,
    "discount": args.discount,
    "tau": args.tau,
}

# Initialize policy
if args.policy == "TD3":
    # Target policy smoothing is scaled wrt the action scale
    kwargs["policy_noise"] = args.policy_noise * max_action
    kwargs["noise_clip"] = args.noise_clip * max_action
    kwargs["policy_freq"] = args.policy_freq
    policy = TD3.TD3(**kwargs)
elif args.policy == "OurDDPG":
    policy = OurDDPG.DDPG(**kwargs)
elif args.policy == "DDPG":
    policy = DDPG.DDPG(**kwargs)

if args.load_model != "":
    policy_file = file_name if args.load_model == "default" else args.load_model
    policy.load(f"./models/{policy_file}")

replay_buffer = utils.ReplayBuffer(state_dim, action_dim)


# ---- 用pymysql 操作数据库
def get_connection():
    conn = pymysql.connect(host=host, port=port, db=db, user=user, password=password)
    return conn


# Runs policy for X episodes and returns average reward
# A fixed seed is used for the eval environment
def eval_policy(policy, env_name, seed, eval_episodes=10):
    eval_env = gym.make(env_name)
    eval_env.seed(seed + 100)

    avg_reward = 0.
    for _ in range(eval_episodes):
        state, done = eval_env.reset(), False
        while not done:
            action = policy.select_action(np.array(state))
            state, reward, done, _ = eval_env.step(action)
            avg_reward += reward

    avg_reward /= eval_episodes

    print("---------------------------------------")
    print(f"Evaluation over {eval_episodes} episodes: {avg_reward:.3f}")
    print("---------------------------------------")
    return avg_reward


# Evaluate untrained policy
# evaluations = [eval_policy(policy, args.env, args.seed)]

state, done = env.reset(), False
episode_reward = 0
episode_timesteps = 0
episode_num = 0
Conn = get_connection()
cursor = Conn.cursor(pymysql.cursors.DictCursor)
Conn.autocommit = True


def generate_setpoint(step):
    global state, episode_timesteps
    episode_timesteps += 1

    # Select action randomly or according to policy
    if step < args.start_timesteps:
        action = env.action_space.sample()
    else:
        action = (
                policy.select_action(np.array(state))
                + np.random.normal(0, max_action * args.expl_noise, size=action_dim)
        ).clip(-max_action, max_action)

    # Store action
    setpoint = min(max(action[0], env.min_setpoint), env.max_setpoint)
    sql_insert = "insert into setpoint values(null, %s)"
    cursor.execute(sql_insert, setpoint)
    Conn.commit()


def read_state(step, cur_action):
    cur_action = np.array([cur_action])

    # Perform action
    global state, done, episode_timesteps, episode_reward, episode_num
    next_state, reward, done, _ = env.step(cur_action)
    done_bool = float(done) if episode_timesteps < env.spec.max_episode_steps else 0

    # Read new state
    sql_query = "SELECT PMV, Energy_consumption FROM thermal_state WHERE ID = (SELECT MAX(ID) FROM thermal_state)"
    cursor.execute(sql_query)
    data = cursor.fetchone()
    Conn.commit()
    PMV = data['PMV']
    Energy = data['Energy_consumption']
    next_state = np.array([PMV, Energy], dtype=np.float32)
    # print(f"energy: {Energy} reward: {reward}")
    # Update the state of env
    env.state[0] = PMV
    env.state[1] = Energy

    # Store data in replay buffer
    replay_buffer.add(state, cur_action, next_state, reward, done_bool)

    state = next_state
    episode_reward += reward

    # Train agent after collecting sufficient data
    if step >= args.start_timesteps:
        policy.train(replay_buffer, args.batch_size)

    if done:
        # +1 to account for 0 indexing. +0 on ep_timesteps since it will increment +1 even if done=True
        sql_reward = "insert into episode_reward values(null, %s, %s, %s)"
        cursor.execute(sql_reward, step, episode_num + 1, episode_reward)
        Conn.commit()
        # Reset environment
        state, done = env.reset(), False
        episode_reward = 0
        episode_timesteps = 0
        episode_num += 1

    # Evaluate episode
    """"
    if (step + 1) % args.eval_freq == 0:
        evaluations.append(eval_policy(policy, args.env, args.seed))
        np.save(f"./results/{file_name}", evaluations)
        if args.save_model: policy.save(f"./models/{file_name}")
    """