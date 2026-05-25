"""
机器学习因子模型层

模块组成：
  - features.py    — 特征工程（从原始市场数据提取 12+ 标准化因子）
  - model.py       — XGBoost 模型训练 / 推理 / 在线更新
  - scorer.py      — ML 评分服务（替代线性 signal_score）
  - dataset.py     — 训练数据集构建（历史 signal → outcome 标注）

使用流程：
  1. dataset.build_training_set() → 从历史交易生成训练集
  2. model.train() → 训练 XGBoost 模型
  3. scorer.predict_score(features) → 替代 signal_score.calculate_signal_score()
"""
