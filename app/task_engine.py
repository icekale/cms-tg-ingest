from __future__ import annotations

from .models import RetryAction, RetryDecision, TaskSnapshot, TaskStage, TaskStatus

_STAGE_NAMES = {
    TaskStage.RECEIVED: "收到链接",
    TaskStage.CMS_SUBMITTED: "提交 CMS",
    TaskStage.ORGANIZED: "CMS 整理",
    TaskStage.OWN_SHARE_CREATED: "创建自有分享",
    TaskStage.SHARE_SYNC_SUBMITTED: "分享同步",
    TaskStage.STRM_READY: "STRM 生成",
    TaskStage.MOVED: "移动媒体库",
    TaskStage.EMBY_CONFIRMED: "Emby 确认",
    TaskStage.CLEANED: "清理转存源",
    TaskStage.NEEDS_ACTION: "等待人工处理",
    TaskStage.FAILED: "失败",
}

_RETRYABLE_STAGES = {
    TaskStage.CMS_SUBMITTED,
    TaskStage.ORGANIZED,
    TaskStage.OWN_SHARE_CREATED,
    TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.STRM_READY,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
    TaskStage.CLEANED,
}


def stage_display_name(stage: TaskStage) -> str:
    return _STAGE_NAMES.get(stage, stage.value)


def decide_retry(task: TaskSnapshot, max_retries: int = 3) -> RetryDecision:
    if task.current_stage == TaskStage.CLEANED and task.status == TaskStatus.SUCCEEDED:
        return RetryDecision(RetryAction.NO_RETRY, None, "任务已完成，无需重试")
    if task.current_stage == TaskStage.NEEDS_ACTION or task.status == TaskStatus.NEEDS_ACTION:
        return RetryDecision(RetryAction.MANUAL_ACTION_REQUIRED, None, "任务需要人工选择或确认")
    if task.retry_count >= max_retries:
        return RetryDecision(RetryAction.MANUAL_ACTION_REQUIRED, None, f"重试次数超过限制 {max_retries} 次")
    if task.current_stage in _RETRYABLE_STAGES:
        name = stage_display_name(task.current_stage)
        return RetryDecision(RetryAction.RETRY_CURRENT_STAGE, task.current_stage, f"将从当前阶段重试：{name}")
    return RetryDecision(RetryAction.NO_RETRY, None, "当前阶段不支持自动重试")
