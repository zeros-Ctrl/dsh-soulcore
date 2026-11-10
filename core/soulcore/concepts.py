"""
概念词典：把"意思相同、用词不同"的说法映射到同一个概念 ID（cpt0、cpt1……），
文档和查询两侧都加上概念 token，这样"向量库"能召回"embedding"、"陀螺仪"能召回"IMU"。

这是离线、零依赖的兜底方案；配置了 embedding 接口时会再叠加真正的语义向量检索。
想补充词条：往 GROUPS 里加一行即可（同一行里的词互为近义）。英文一律小写。
"""
from __future__ import annotations

GROUPS = [
    # ── 存储 / 数据 ──
    "数据库 db database sqlite mysql postgres postgresql mongodb redis 存储 持久化 storage",
    "向量 向量库 向量数据库 embedding embeddings 嵌入 milvus faiss chroma qdrant 语义检索 语义搜索",
    "检索 搜索 查找 召回 search retrieval 全文检索 fts fts5 索引 index",
    "备份 backup 容灾 恢复 还原 快照 snapshot 找回 数据丢失 丢了",
    "缓存 cache 缓冲",
    "日志 log logs logging jsonl 记录",
    "表格 excel xlsx csv 电子表格",
    # ── 开发 ──
    "前端 界面 ui 页面 网页 frontend vue react css html",
    "后端 服务端 backend server api 接口 fastapi flask django",
    "服务器 主机 机器 硬件 设备 板子 开发板 主控 主控板 树莓派 香橙派 rdk 托管 部署在",
    "部署 上线 发布 deploy release 托管 运行在 跑在 docker 容器",
    "报错 错误 bug 异常 崩溃 crash error exception 故障 问题",
    "测试 test tests 单测 单元测试 pytest 自测 跑测试",
    "提交 commit push 推送 合并 merge pr",
    "代码 程序 脚本 code 源码",
    "编程语言 语言 python java javascript js typescript ts c++ go rust",
    "画图 绘图 图表 作图 可视化 plot matplotlib echarts 画",
    "字体 字形 font 黑体 宋体",
    "主题 配色 皮肤 颜色 theme 深色 暗色 夜间模式 黑色背景 dark darkmode 白色界面 浅色 亮色",
    "性能 速度 慢 卡 延迟 latency 快",
    "配置 设置 config settings 参数",
    "开源 github git 仓库 repo",
    # ── 网站 / 社区 ──
    "博客 网站 站点 blog site 个人网站 主页",
    "评论 留言 回复 comment 弹幕",
    "广告 垃圾留言 垃圾评论 spam 水军 灌水 刷屏",
    "审核 审批 管理员审核 人工审核 过审 moderation",
    "注册 账号 用户 新用户 开户 sign up signup register",
    "上限 限制 最多 封顶 配额 quota limit",
    # ── 机器人 ──
    "机器人 小车 车子 robot 底盘 无人车",
    "导航 路径规划 nav2 navigation 规划",
    "定位 amcl slam 位置 在哪 知道自己在哪 localization",
    "避障 障碍物 防撞 obstacle 绕障",
    "激光雷达 雷达 激光 lidar 测距",
    "摄像头 相机 视觉 camera 图像 镜头 视频流",
    "imu 陀螺仪 加速度计 惯性测量单元 惯导 bmi088",
    "里程计 odom odometry 编码器 轮速",
    "漂移 偏航 跑偏 转向不准 转弯 拐弯 偏移 drift",
    "标定 校准 calibration 校正",
    "总线 i2c spi uart 串口 接在 挂在",
    # ── 学业 ──
    "毕设 毕业设计 论文 毕业论文 thesis",
    "导师 老师 指导老师 教授 指导",
    "组会 开会 会议 例会 汇报会 meeting",
    "报告 汇报 进度汇报 中期报告 材料 report",
    "答辩 中期 中期答辩 开题 defense",
    "截止 deadline ddl 之前 截至 交 提交 期限 到期",
    "数据集 测试集 训练集 样本 标注 dataset",
    "专业 系 学院 方向 major",
    "大一 大二 大三 大四 年级 研一 研二 研究生 本科",
    "考试 考 测验 期末 期中",
    # ── 情绪 / 人格 ──
    "情绪 心情 情感 mood 开心 难过 emotion",
    "平滑 跳变 突变 波动 稳定 pid 渐变 突然",
    "人格 人设 性格 persona 角色",
    "记忆 记住 记得 memory 回忆",
    # ── 生活 ──
    "生日 出生 过生 诞辰 birthday 几月几号",
    "吃 饮食 外卖 点餐 忌口 口味 饭 菜",
    "香菜 葱 蒜 辣 忌口",
    "猫 猫咪 橘猫 宠物 喵 团子 狗",
    "睡觉 半夜 深夜 晚上 熬夜 凌晨 12点后 打扰 休息",
    "游戏 开黑 打游戏 瓦 瓦罗兰特 lol 原神 打瓦",
    "视频 b站 bilibili 鬼畜 科技区 刷视频 番剧",
    "音乐 歌 听歌 网易云",
    "实习 工作 上班 offer 求职",
    "周末 周六 周日 星期六 星期日 礼拜六",
    "星期一 周一",
    "星期三 周三",
    "星期五 周五",
    "钱 余额 充值 费用 价格 花销",
    "提醒 提示 通知 叫我 别忘",
    "喜欢 偏好 爱 中意 习惯",
    "讨厌 不喜欢 烦 受不了 反感 刺眼",
    "深圳 广州 北京 上海 杭州 城市",
]

_INDEX: dict[str, list[int]] = {}
for gid, line in enumerate(GROUPS):
    for term in line.split():
        _INDEX.setdefault(term.lower(), []).append(gid)
_TERMS = sorted(_INDEX, key=len, reverse=True)


_DISABLED = bool(__import__("os").environ.get("SOULCORE_NO_CONCEPTS"))   # 仅用于消融实验


def concepts(text: str) -> list[str]:
    """返回文本里出现的概念 token（cptN），按出现去重"""
    if _DISABLED:
        return []
    t = (text or "").lower()
    found: list[int] = []
    taken = [False] * len(t)
    for term in _TERMS:
        start = 0
        while True:
            i = t.find(term, start)
            if i < 0:
                break
            # 英文词要求词边界，避免 "ts" 命中 "tests"
            if term.isascii():
                l_ok = i == 0 or not t[i - 1].isalnum()
                r_ok = i + len(term) == len(t) or not t[i + len(term)].isalnum()
                if not (l_ok and r_ok):
                    start = i + 1
                    continue
            if not any(taken[i:i + len(term)]):
                for j in range(i, i + len(term)):
                    taken[j] = True
                for g in _INDEX[term]:
                    if g not in found:
                        found.append(g)
            start = i + len(term)
    return [f"cpt{g}" for g in found]
