# -*- coding: utf-8 -*-
# 乘起来为0.05,在乘以该系数得到0.25,q值可能在0.5浮动
board_size = 9  # 棋盘大小
simulation_per_step = 700  # AlphaGo Zero这里取值为1600
goal = 5  # 先学三子棋
batch_size = 256  # AlphaGo Zero这里取值为2048
lr_ = [(200, 2e-4), (1200, 2e-3), (2800, 2e-4), (4000, 2e-5), (100000000, 2e-6)]
discount = None  # 折扣系数，None就是没有折扣
ckpt_path = "ckpt"
temperature = 0.5
total_step = 8000
tau_decay_rate = 0.9  # 温度衰减项，越往后温度应该越低
# 噪声项
noise_eps = 0.25
c_puct = 4.0
dirichlet_alpha = 0.5  # 这个值越大，产生的分布越均匀
gamma = 0.95  # 权重衰减因子
init_temp = 2.0  # 初始温度  # 温度越大分布越平缓


def get_lr(step):
    for item in lr_:
        if step < item[0]:
            return item[1]
    return lr_[-1][-1]
