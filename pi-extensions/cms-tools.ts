// cms-tg-ingest 助手工具：只读查询走 assistant_read.py（SELECT），
// 动作走 assistant_ops.py → apply_task_action。随镜像发布。
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  formatSize,
  truncateHead,
  truncateTail,
} from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { execFile } from "node:child_process";

const READ_SCRIPT = process.env.CMS_TOOLS_SCRIPT || "/app/scripts/assistant_read.py";
const OPS_SCRIPT = process.env.CMS_TOOLS_OPS_SCRIPT || "/app/scripts/assistant_ops.py";
const TASK_ACTIONS = ["retry", "emby", "restore", "reprocess", "resume_organizing", "terminate"] as const;
const TASK_STATUSES = ["pending", "running", "succeeded", "failed", "needs_action", "cancelled"] as const;
const PRUNE_TOOLS = new Set(["task_detail", "query_tasks", "task_events", "system_stats"]);
const CONFIRM_RE = /确认|好的|执行|可以|同意|yes|\bok\b/i;
const SNAPSHOT_MARK = "---- 系统快照";

function run(script: string, args: string[], signal?: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "python3",
      [script, ...args],
      { timeout: 25_000, maxBuffer: 4 * 1024 * 1024, signal },
      (err, stdout, stderr) => {
        if (err) {
          reject(new Error(String(stderr || err.message).slice(0, 500)));
        } else {
          resolve(String(stdout || ""));
        }
      },
    );
  });
}

const read = (args: string[], signal?: AbortSignal) => run(READ_SCRIPT, args, signal);

function coerceIds(args: Record<string, unknown> | undefined) {
  if (!args || typeof args !== "object") return args;
  const out: Record<string, unknown> = { ...args };
  for (const key of ["task_id", "limit"] as const) {
    if (out[key] == null || out[key] === "") continue;
    const n = Number(out[key]);
    if (Number.isFinite(n)) out[key] = Math.trunc(n);
  }
  if (out.status === "") delete out.status;
  return out;
}

function textToolResult(text: string, mode: "head" | "tail" = "head") {
  const truncation = (mode === "tail" ? truncateTail : truncateHead)(String(text ?? ""), {
    maxLines: DEFAULT_MAX_LINES,
    maxBytes: DEFAULT_MAX_BYTES,
  });
  let out = truncation.content;
  if (truncation.truncated) {
    out += `\n\n[truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}]`;
  }
  return { content: [{ type: "text" as const, text: out }], details: {} };
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => (part?.type === "text" ? String(part.text || "") : "")).join("");
}

function asMessage(entry: { type?: string; message?: { role?: string; content?: unknown }; role?: string; content?: unknown }) {
  return entry?.message && (entry.type === "message" || entry.message.role) ? entry.message : entry;
}

function lastUserQuestion(ctx: { sessionManager?: { getBranch?: () => unknown[] } }): string {
  const entries = ctx.sessionManager?.getBranch?.() ?? [];
  for (let i = entries.length - 1; i >= 0; i--) {
    const msg = asMessage(entries[i] as { type?: string; message?: { role?: string; content?: unknown } });
    if (msg?.role !== "user") continue;
    const text = messageText(msg.content);
    const cut = text.indexOf(SNAPSHOT_MARK);
    return (cut >= 0 ? text.slice(0, cut) : text).trim();
  }
  return "";
}

function pruneMessages(messages: Array<{ role?: string; toolName?: string; content?: unknown }>) {
  const lastIdx = new Map<string, number>();
  let lastUser = -1;
  messages.forEach((msg, i) => {
    if (msg?.role === "user") lastUser = i;
    if (msg?.role === "toolResult" && PRUNE_TOOLS.has(String(msg.toolName || ""))) {
      lastIdx.set(String(msg.toolName), i);
    }
  });
  return messages.map((msg, i) => {
    if (msg?.role === "toolResult" && PRUNE_TOOLS.has(String(msg.toolName || "")) && lastIdx.get(String(msg.toolName)) !== i) {
      return { ...msg, content: [{ type: "text", text: `[stale ${msg.toolName} result omitted]` }] };
    }
    if (msg?.role === "user" && i !== lastUser) {
      const text = messageText(msg.content);
      const cut = text.indexOf(SNAPSHOT_MARK);
      if (cut >= 0) {
        return { ...msg, content: `${text.slice(0, cut).trimEnd()}\n\n[stale snapshot omitted]` };
      }
    }
    return msg;
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "task_detail",
    label: "任务详情",
    description:
      "查询 cms-tg-ingest 指定任务的完整详情：状态、阶段、整理目标、完整事件流。用户问某个具体任务（如“#445 怎么了”）时用它获取实时数据。",
    parameters: Type.Object({
      task_id: Type.Integer({ description: "任务 ID，例如 445" }),
    }),
    prepareArguments: coerceIds,
    async execute(_toolCallId, params, signal) {
      return textToolResult(await read(["task", String(params.task_id)], signal));
    },
  });

  pi.registerTool({
    name: "query_tasks",
    label: "查询任务列表",
    description: "按状态筛选最近任务列表。回答“现在有哪些任务”类问题时使用。",
    parameters: Type.Object({
      status: Type.Optional(StringEnum(TASK_STATUSES)),
      limit: Type.Optional(Type.Integer({ description: "返回条数，默认 15，最大 200" })),
    }),
    prepareArguments: coerceIds,
    async execute(_toolCallId, params, signal) {
      const args = ["tasks"];
      if (params.status) args.push("--status", String(params.status));
      args.push("--limit", String(params.limit ?? 15));
      return textToolResult(await read(args, signal));
    },
  });

  pi.registerTool({
    name: "task_events",
    label: "任务事件流",
    description: "查询指定任务的事件流水（每个阶段的发生记录与失败原因），诊断任务历史时使用。",
    parameters: Type.Object({
      task_id: Type.Integer({ description: "任务 ID" }),
      limit: Type.Optional(Type.Integer({ description: "返回条数，默认 20" })),
    }),
    prepareArguments: coerceIds,
    async execute(_toolCallId, params, signal) {
      return textToolResult(
        await read(["events", String(params.task_id), "--limit", String(params.limit ?? 20)], signal),
        "tail",
      );
    },
  });

  pi.registerTool({
    name: "system_stats",
    label: "系统统计",
    description:
      "最近 200 个任务的状态分布、needs_action 清单等系统级统计。用户问“系统整体怎么样/有什么积压”时使用。",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, signal) {
      return textToolResult(await read(["stats"], signal));
    },
  });

  pi.registerTool({
    name: "task_action",
    label: "执行任务操作",
    description:
      "对任务执行修复操作，与 Web 管理台按钮同一入口（带资格校验）。动作：" +
      "retry=从失败点重试；reprocess=从头重跑；resume_organizing=继续整理；" +
      "emby=重新确认 Emby 入库；restore=恢复 STRM；terminate=终止任务（破坏性）。" +
      "用户只是问“该怎么办”时不要调用。执行后如实报告 applied 与 reason。",
    promptGuidelines: [
      "Call task_action only after the user's latest message clearly agrees (确认/好的/执行/yes/ok). If they only asked what to do, advise first.",
      "task_action terminate is blocked unless that latest user message contains a confirmation word.",
    ],
    parameters: Type.Object({
      task_id: Type.Integer({ description: "任务 ID" }),
      action: StringEnum(TASK_ACTIONS),
    }),
    prepareArguments: coerceIds,
    async execute(_toolCallId, params, signal) {
      return textToolResult(await run(OPS_SCRIPT, ["act", String(params.task_id), String(params.action)], signal));
    },
  });

  pi.on("tool_call", (event, ctx) => {
    if (event.toolName !== "task_action") return;
    if (String(event.input?.action ?? "") !== "terminate") return;
    if (CONFIRM_RE.test(lastUserQuestion(ctx))) return;
    return { block: true, reason: "task_action terminate 需要用户本轮明确确认（确认/好的/执行/yes/ok）" };
  });

  pi.on("context", (event) => ({ messages: pruneMessages(event.messages) }));
}
