# -*- coding: utf-8 -*-
from collections import defaultdict
import utils
import numpy as np
import random
import config
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED, FIRST_COMPLETED
from multiprocessing import Process
import threading


class Action(object):
    def __init__(self):
        self.n = 0  # 初始化为1，使得概率分布smooth一丢丢
        self.w = 0  # W(s, a) : total action value
        self.q = 0  # Q(s, a) = N / W : action value
        self.p = 0  # P(s, a) : prior probability


class State(object):
    def __init__(self):
        self.a = defaultdict(Action)  # key: action, value: ActionState only valid action included
        self.sum_n = 0  # visit count


class Player(object):
    def __init__(self, cfg=None, training=True, pv_fn=None, use_net=True, q_metric=False):
        self.config = config
        self.q_metric = q_metric
        self.pv_fn = pv_fn
        self.training = training
        # 做成这个样子而不是树状，是因为不同的action序列可能最终得到同一state，做成树状就不利于搜索信息的搜集
        self.tree = defaultdict(State)  # 一个字符串表示的状态到包含信息的状态的映射
        self.root_state = None
        self.use_net = use_net  # True表示使用神经网络，False表示纯mcts
        self.goal = self.config.goal
        self.tau = self.config.init_temp  # 初始温度
        self.all_done = Lock()
        self.node_lock = defaultdict(Lock)  # key: state key, value: Lock of that state
        self.executor = ThreadPoolExecutor(max_workers=self.config.search_threads)

    def get_init_state(self):
        """
        用一个字符串表示棋盘，从上至下从左至由编码
        黑子用3白字用1表示，空格部分用小写字母表示，a表示0个连续空格，b表示一个连续空格，以此类推
        :return:
        """
        fen = ""
        for i in range(self.config.board_size):
            fen += chr(ord("a") + self.config.board_size) + '/'
        return fen

    def reset(self, search_tree=None):
        self.tree = defaultdict(State) if search_tree is None else search_tree
        self.root_state = None
        self.tau = self.config.init_temp  # 初始温度
        self.executor = ThreadPoolExecutor(max_workers=self.config.search_threads)
        # self.visited_states = defaultdict(list)

    def run(self, e=0.25):
        """
        对弈一局，
        :return:
        """
        state = self.get_init_state()
        game_over = False
        data = []  # 收集(状态，动作)二元组
        value = 0
        while not game_over:
            policy, action = self.get_action(state, e)
            data.append((state, policy))  # 装初始局面不装最终局面，装的是动作执行之前的局面
            board = utils.step(utils.state_to_board(state, self.config.board_size), action)
            state = utils.board_to_state(board)
            self.pruning_tree(board, state)  # 走完一步以后，对其他分支进行剪枝，以节约内存；注释掉，以节约时间
            game_over, value = utils.is_game_over(board, self.goal)

        self.reset()  # 把树重启
        turns = len(data)
        if turns % 2 == 1:
            value = -value
        weights = utils.construct_weights(turns, gamma=self.config.gamma)
        final_data = []
        for i in range(turns):
            final_data.append((*data[i], value, weights[i]))  # (状态，policy，value， weight)
            value = -value
        return final_data

    def calc_policy(self, state, e):
        """
        根据访问次数计算策略和动作，
        :param state:
        :return: 策略，最佳动作，随机动作
        """
        node = self.tree[state]
        policy = np.zeros((self.config.board_size, self.config.board_size), np.float32)
        most_visit_count = -1
        candidate_actions = list(node.a.keys())
        policy_valid = np.empty((len(candidate_actions),), dtype=np.float32)
        for i, action in enumerate(candidate_actions):
            policy_valid[i] = node.a[action].n
            most_visit_count = node.a[action].n if node.a[action].n > most_visit_count else most_visit_count
        best_moves = [action for action in candidate_actions if node.a[action].n == most_visit_count]
        best_move = random.choice(best_moves)
        self.tau *= self.config.tau_decay_rate
        if self.tau < 0.01:
            for mv in best_moves:
                policy[mv[0], mv[1]] = 1.0 / len(best_moves)
            return policy, best_move, best_move
        # 需要先归一化，再取温度，再归一化。否则温度指数容易溢出
        # policy_valid /= np.maximum(np.sum(policy_valid), 1e-3)
        policy_valid /= np.max(policy_valid)  # 除以最大值就好了，除以和的话，向下精度损失较大
        policy_valid = np.power(policy_valid, 1 / self.tau)
        policy_valid /= np.sum(policy_valid)
        for i, action in enumerate(candidate_actions):
            policy[action[0], action[1]] = policy_valid[i]
        p = (1 - e) * policy_valid + e * np.random.dirichlet(0.5 * np.ones(policy_valid.shape[0]))
        p = p / p.sum()  # 有精度损失，导致其和不是1了
        random_action = candidate_actions[int(np.random.choice(len(candidate_actions), p=p))]
        return policy, best_move, random_action

    def calc_policy_q(self, state):
        """
        根据q值来计算策略，
        :param state:
        :return: 策略，最佳动作，随机动作
        """
        node = self.tree[state]
        policy = np.zeros((self.config.board_size, self.config.board_size), np.float32)
        # most_visit_count = -1
        candidate_actions = list(node.a.keys())
        policy_valid = np.empty((len(candidate_actions),), dtype=np.float32)
        for i, action in enumerate(candidate_actions):
            policy_valid[i] = node.a[action].q
        best_move = candidate_actions[policy_valid.argmax()]
        self.tau *= self.config.tau_decay_rate
        if self.tau < 0.01:
            policy[best_move[0], best_move[1]] = 1.0
            return policy, best_move
        policy_valid = softmax(policy_valid / self.tau)
        random_action = candidate_actions[int(np.random.choice(len(candidate_actions), p=policy_valid))]
        for i, action in enumerate(candidate_actions):
            policy[action[0], action[1]] = policy_valid[i]
        return policy, best_move, random_action

    def get_action(self, state, e=0.25):
        """
        这一部分是最耗时的，采用Tree parallelization来做，多线程异步搜索
        :param state:
        :param e:
        :return:
        """
        # self.all_done.acquire(True)  # 以阻塞方式获取线程锁。事实上也没必要，多线程没有在这一步操作。只是以防万一
        self.root_state = state
        num_task = self.config.simulation_per_step//self.config.search_threads  # 要执行这么多搜索任务
        threads = []
        procs = []
        for i in range(self.config.search_threads):
            t = threading.Thread(target=self.MCTS_search, args=(state, [state], num_task))
            t.start()
            threads.append(t)
            # proc = Process(target=self.MCTS_search, args=(state, [state], num_task))
            # proc.start()
            # procs.append(proc)
            # threads.append(self.executor.submit(self.MCTS_search, state, [state], num_task))
        # wait(threads, return_when=ALL_COMPLETED)  # 等待所有的线程执行完毕
        # self.all_done.release()

        for p in threads:
            p.join()
        policy, best_action, random_action = self.calc_policy(state, e)
        if self.training:
            action = random_action
        else:
            action = best_action
        return policy, action

    def pruning_tree(self, board: np.ndarray, state: str = None):
        """
        基于board，对树进行剪枝，把board的祖先状态全部剪掉
        :param board:
        :return:
        """
        if state is None:
            state = utils.board_to_state(board)
        keys = list(self.tree.keys())
        for key in keys:
            b = utils.state_to_board(key, self.config.board_size)
            if key != state \
                    and np.all(np.where(board == 1, 1, 0) >= np.where(b == 1, 1, 0)) \
                    and np.all(np.where(board == -1, 1, 0) >= np.where(b == -1, 1, 0)):
                del self.tree[key]

    def update_tree(self, v, history: list):
        """
        :param p: policy 当前局面对黑方的策略
        :param v: value, 当前局面对黑方的价值
        :param history: 包含当前局面的一个棋局，(state, action) pair
        :return:
        """
        _ = history.pop()  # 最近的棋局
        #  注意，这里并没有把v赋给当前node
        virtual_loss = self.config.virtual_loss
        while len(history) > 0:
            action = history.pop()
            state = history.pop()
            v = -v
            with self.node_lock[state]:
                # 修改结点信息，需要加锁
                node = self.tree[state]  # 状态结点
                action_state = node.a[action]  # 该状态下的action边
                action_state.n += 1 - virtual_loss
                action_state.w += v + virtual_loss
                action_state.q = action_state.w / action_state.n

    def evaluate_and_expand(self, state: str, board: np.ndarray = None):
        if board is None:
            board = utils.state_to_board(state, self.config.board_size)
        policy, value = self.pv_fn(utils.board_to_inputs(board)[np.newaxis, ...])
        legal_moves = utils.get_legal_moves(board)
        all_p = max(sum([policy[0, action[0] * self.config.board_size + action[1]] for action in legal_moves]), 1e-5)
        for action in legal_moves:
            self.tree[state].a[action].p = policy[0, action[0] * self.config.board_size + action[1]] / all_p
        return value[0]  # 去掉batch维度，故去[0]

    def expand(self, state: str, board: np.ndarray = None):
        if board is None:
            board = utils.state_to_board(state, self.config.board_size)
        self.tree[state].sum_n = 1
        legal_moves = utils.get_legal_moves(board)
        all_p = len(legal_moves)
        for action in legal_moves:
            self.tree[state].a[action].p = 1.0 / all_p

    def MCTS_search(self, state: str, history: list, num:int):
        """
        从state出发进行num次MCTS搜索，每次搜完以后形成一棵树
        :param state:
        :param history:
        :return:
        """
        origin_state, origin_history = state, history
        virtual_loss = self.config.virtual_loss
        for _ in range(num):
            state, history = origin_state, origin_history.copy()
            while True:
                board = utils.state_to_board(state, self.config.board_size)
                game_over, v = utils.is_game_over(board, self.goal)  # 落子前检查game over
                if game_over:
                    self.update_tree(v, history=history)
                    break
                with self.node_lock[state]:
                    if state not in self.tree:
                        # 未出现过的state，则评估然后展开
                        v = self.evaluate_and_expand(state, board)  # 落子前进行评估
                        self.update_tree(v, history=history)
                        break
                    sel_action = self.select_action_q_and_u(state)
                    action_state = self.tree[state].a[sel_action]
                    action_state.n += virtual_loss
                    action_state.w -= virtual_loss
                    action_state.q = action_state.w / action_state.n
                history.append(sel_action)
                board = utils.step(board, sel_action)
                state = utils.board_to_state(board)
                history.append(state)

    def pure_MCTS_search(self, state: str, history: list):
        """
        从state出发进行一次MCTS搜索，搜完以后形成一棵树, 没有使用net
        :param state:
        :param history:
        :return:
        """
        while True:
            board = utils.state_to_board(state, self.config.board_size)
            game_over, v = utils.is_game_over(board, self.goal)  # 落子前检查game over
            if game_over:
                self.update_tree(v, history=history)
                break
            if state not in self.tree:
                self.expand(state, board)  # 没出现，则展开
            sel_action = self.select_action_q_and_u(state)
            history.append(sel_action)
            board = utils.step(board, sel_action)
            state = utils.board_to_state(board)
            history.append(state)

    def UCB_value(self, node, action):
        pass

    def select_action_q_and_u(self, state: str) -> tuple:
        """
        AlphaZero只在根节点的先验概率处加入dirichlet噪声
        :param state:
        :return:
        """
        node = self.tree[state]
        node.sum_n += 1  # 从这结点出发选择动作，该节点访问次数加一
        action_keys = list(node.a.keys())
        act_count = len(action_keys)
        dirichlet = np.random.dirichlet(self.config.dirichlet_alpha * np.ones(act_count))
        scores = np.empty((act_count,), np.float32)
        # alphaZero只在根节点处加入dirichlet噪声，对先验概率加噪声
        q_value = np.empty((act_count,), np.float32)
        counts = np.empty((act_count,), np.int32)
        best_action = None
        for i, mov in enumerate(action_keys):
            action_state = node.a[mov]
            p_ = action_state.p  # 该动作的先验概率
            if self.root_state == state and self.training:
                # simulation阶段的这个噪声可以防止坍缩，但是收敛却很慢了，噪声系数应该还要调小，或者随着时间逐步减小
                p_ = 0.9 * p_ + 0.1 * dirichlet[i]
            scores[i] = action_state.q + self.config.c_puct * p_ * np.sqrt(node.sum_n + 1) / (1 + action_state.n)
            q_value[i] = action_state.q
            counts[i] = action_state.n
        if self.root_state == state:
            # 对于根节点，保证每个结点至少被访问两次，其中一次是展开，另一次是探索。
            # 故要求simulation_per_step >> 2*board_size*board_size才有意义
            # 这么做使得概率分布更加smooth，并且探索得更好
            no_visits = np.where(counts == 0)[0]
            if no_visits.shape[0] > 0:
                act_idx = np.random.choice(no_visits)
                best_action = action_keys[act_idx]
            else:
                one_visits = np.where(counts == 1)[0]
                if one_visits.shape[0] > 0:
                    act_idx = np.random.choice(one_visits)
                    best_action = action_keys[act_idx]
        if best_action is None:
            max_score = np.max(scores)
            act_idx = np.random.choice([idx for idx in range(act_count) if scores[idx] == max_score])
            best_action = action_keys[act_idx]
        return best_action


def softmax(x):
    m = np.max(x)
    x -= m
    ex = np.exp(x)
    fenmu = np.sum(ex)
    return ex / fenmu


def my_pow(x: float, y: int):
    res = x
    while y > 0:
        res *= x
        y -= 1
    return x
