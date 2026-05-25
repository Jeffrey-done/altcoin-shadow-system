#!/usr/bin/env python3
"""
ML 信号模型 — XGBoost 二分类（盈利概率预测）

模型目标：
  给定 12 维特征向量，预测"开仓后 24h 内盈利"的概率 P(profit)。
  - P > 0.65 → 高置信度开仓（全仓）
  - 0.50 < P < 0.65 → 中等信号（半仓）
  - P < 0.50 → 跳过

训练流程：
  1. 从历史交易构建训练集（features + label: 1=盈利, 0=亏损）
  2. 80/20 时间切分（不 shuffle，保证无 look-ahead）
  3. XGBoost 训练 + 交叉验证
  4. 保存模型到 ml/models/signal_model.json

推理流程：
  1. 加载模型（启动时一次）
  2. 实时特征提取 → predict_proba → 返回盈利概率

在线更新：
  - 每周用最新 N 笔交易 retrain（Walk-forward）
  - 旧模型保留备份，新模型 A/B 测试 7 天后切换
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("ml.model")

# 模型存储路径
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MODELS_DIR = os.path.join(_SCRIPT_DIR, 'models')
_DEFAULT_MODEL_PATH = os.path.join(_MODELS_DIR, 'signal_model.json')


@dataclass
class ModelConfig:
    """模型配置"""
    # XGBoost 超参数
    n_estimators: int = 200
    max_depth: int = 5
    learning_rate: float = 0.05
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 3
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0

    # 训练配置
    min_train_samples: int = 50        # 至少 N 笔交易才训练
    val_ratio: float = 0.2             # 验证集比例
    early_stopping_rounds: int = 20

    # 推理阈值
    high_confidence_threshold: float = 0.65
    medium_confidence_threshold: float = 0.50

    # 在线更新
    retrain_interval_days: int = 7
    min_new_samples_for_retrain: int = 20


@dataclass
class PredictionResult:
    """模型预测结果"""
    profit_probability: float = 0.5    # P(profit) ∈ [0, 1]
    confidence: str = 'low'            # 'high' / 'medium' / 'low'
    suggested_stake_ratio: float = 0.0 # 建议仓位比例 (0~1)
    model_version: str = ''
    feature_importance_top3: List[Tuple[str, float]] = None

    def __post_init__(self):
        if self.feature_importance_top3 is None:
            self.feature_importance_top3 = []


class SignalModel:
    """
    XGBoost 信号预测模型。

    用法:
      model = SignalModel()
      model.load()  # 加载已训练模型

      # 推理
      from ml.features import extract_features
      features = extract_features(rsi_1d=85, ...)
      result = model.predict(features.to_array())
      print(f"盈利概率: {result.profit_probability:.1%}")

      # 训练
      model.train(X_train, y_train, X_val, y_val)
      model.save()
    """

    def __init__(self, config: Optional[ModelConfig] = None):
        self.config = config or ModelConfig()
        self._model = None
        self._model_version = ''
        self._feature_names = None
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded and self._model is not None

    def load(self, path: Optional[str] = None) -> bool:
        """加载已训练模型"""
        model_path = path or _DEFAULT_MODEL_PATH
        if not os.path.exists(model_path):
            logger.info(f"模型文件不存在: {model_path}，将使用 fallback 评分")
            return False

        try:
            import xgboost as xgb
            self._model = xgb.XGBClassifier()
            self._model.load_model(model_path)
            self._loaded = True

            # 读取版本信息
            meta_path = model_path + '.meta'
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                self._model_version = meta.get('version', 'unknown')
                self._feature_names = meta.get('feature_names', None)

            logger.info(f"✅ ML 模型加载成功: v{self._model_version}")
            return True
        except ImportError:
            logger.warning("xgboost 未安装，ML 评分不可用")
            return False
        except Exception as e:
            logger.error(f"模型加载失败: {e}")
            return False

    def predict(self, features: np.ndarray) -> PredictionResult:
        """
        预测盈利概率。

        参数:
          features: shape (12,) 的特征向量

        返回:
          PredictionResult
        """
        cfg = self.config

        if not self.is_loaded:
            # Fallback: 用简单启发式
            return self._fallback_predict(features)

        try:
            # 确保形状正确
            X = features.reshape(1, -1)
            prob = self._model.predict_proba(X)[0, 1]  # P(class=1) = P(profit)

            # 置信度分级
            if prob >= cfg.high_confidence_threshold:
                confidence = 'high'
                stake_ratio = 1.0
            elif prob >= cfg.medium_confidence_threshold:
                confidence = 'medium'
                stake_ratio = 0.5
            else:
                confidence = 'low'
                stake_ratio = 0.0

            # 特征重要性 top 3
            top3 = []
            if hasattr(self._model, 'feature_importances_'):
                from ml.features import FeatureVector
                names = FeatureVector.feature_names()
                importances = self._model.feature_importances_
                indices = np.argsort(importances)[::-1][:3]
                top3 = [(names[i], round(float(importances[i]), 3)) for i in indices]

            return PredictionResult(
                profit_probability=round(float(prob), 4),
                confidence=confidence,
                suggested_stake_ratio=stake_ratio,
                model_version=self._model_version,
                feature_importance_top3=top3,
            )
        except Exception as e:
            logger.warning(f"模型推理异常: {e}")
            return self._fallback_predict(features)

    def train(self, X_train: np.ndarray, y_train: np.ndarray,
              X_val: Optional[np.ndarray] = None,
              y_val: Optional[np.ndarray] = None) -> Dict[str, float]:
        """
        训练模型。

        参数:
          X_train: shape (N, 12) 训练特征
          y_train: shape (N,) 标签 (1=盈利, 0=亏损)
          X_val: 验证集特征（可选）
          y_val: 验证集标签（可选）

        返回:
          {'accuracy': float, 'auc': float, 'f1': float}
        """
        cfg = self.config

        if len(X_train) < cfg.min_train_samples:
            logger.warning(f"训练样本不足: {len(X_train)} < {cfg.min_train_samples}")
            return {'error': 'insufficient_samples'}

        try:
            import xgboost as xgb
            from sklearn.metrics import accuracy_score, roc_auc_score, f1_score

            self._model = xgb.XGBClassifier(
                n_estimators=cfg.n_estimators,
                max_depth=cfg.max_depth,
                learning_rate=cfg.learning_rate,
                subsample=cfg.subsample,
                colsample_bytree=cfg.colsample_bytree,
                min_child_weight=cfg.min_child_weight,
                reg_alpha=cfg.reg_alpha,
                reg_lambda=cfg.reg_lambda,
                use_label_encoder=False,
                eval_metric='logloss',
                random_state=42,
            )

            # 训练
            eval_set = []
            if X_val is not None and y_val is not None:
                eval_set = [(X_val, y_val)]

            self._model.fit(
                X_train, y_train,
                eval_set=eval_set if eval_set else None,
                verbose=False,
            )

            self._loaded = True
            self._model_version = f"v{int(time.time())}"

            # 评估
            metrics = {}
            eval_X = X_val if X_val is not None else X_train
            eval_y = y_val if y_val is not None else y_train

            y_pred = self._model.predict(eval_X)
            y_prob = self._model.predict_proba(eval_X)[:, 1]

            metrics['accuracy'] = round(float(accuracy_score(eval_y, y_pred)), 4)
            metrics['auc'] = round(float(roc_auc_score(eval_y, y_prob)), 4)
            metrics['f1'] = round(float(f1_score(eval_y, y_pred)), 4)
            metrics['train_samples'] = len(X_train)
            metrics['val_samples'] = len(eval_X)
            metrics['model_version'] = self._model_version

            logger.info(
                f"📊 模型训练完成: accuracy={metrics['accuracy']:.1%} "
                f"AUC={metrics['auc']:.3f} F1={metrics['f1']:.3f} "
                f"(train={len(X_train)}, val={len(eval_X)})"
            )
            return metrics

        except ImportError:
            logger.error("xgboost 或 sklearn 未安装")
            return {'error': 'missing_dependencies'}
        except Exception as e:
            logger.error(f"模型训练异常: {e}", exc_info=True)
            return {'error': str(e)}

    def save(self, path: Optional[str] = None) -> bool:
        """保存模型到文件"""
        if not self.is_loaded:
            logger.warning("无模型可保存")
            return False

        model_path = path or _DEFAULT_MODEL_PATH
        os.makedirs(os.path.dirname(model_path), exist_ok=True)

        try:
            self._model.save_model(model_path)

            # 保存元信息
            from ml.features import FeatureVector
            meta = {
                'version': self._model_version,
                'feature_names': FeatureVector.feature_names(),
                'saved_at': time.time(),
                'n_features': 12,
            }
            with open(model_path + '.meta', 'w') as f:
                json.dump(meta, f, indent=2)

            logger.info(f"模型已保存: {model_path}")
            return True
        except Exception as e:
            logger.error(f"模型保存失败: {e}")
            return False

    def _fallback_predict(self, features: np.ndarray) -> PredictionResult:
        """Fallback：无模型时用简单启发式"""
        # 基于 RSI + 涨幅 + OI 的简单规则
        rsi = features[0]  # rsi_1d_normalized
        pct = features[2]  # pct_24h normalized
        oi = features[4]   # oi_change

        # 做空逻辑：RSI 越高 + 涨幅越大 + OI 越高 → 越可能盈利
        pseudo_prob = 0.3 + rsi * 0.3 + max(0, pct) * 0.2 + oi * 0.2
        pseudo_prob = max(0.1, min(0.9, pseudo_prob))

        if pseudo_prob >= 0.65:
            confidence = 'high'
            stake = 1.0
        elif pseudo_prob >= 0.50:
            confidence = 'medium'
            stake = 0.5
        else:
            confidence = 'low'
            stake = 0.0

        return PredictionResult(
            profit_probability=round(pseudo_prob, 4),
            confidence=confidence,
            suggested_stake_ratio=stake,
            model_version='fallback_heuristic',
        )


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_model: Optional[SignalModel] = None


def get_signal_model() -> SignalModel:
    """获取全局模型单例"""
    global _model
    if _model is None:
        _model = SignalModel()
        _model.load()
    return _model
