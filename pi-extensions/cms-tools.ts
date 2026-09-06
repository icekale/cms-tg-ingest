// cms-tg-ingest 助手只读工具：让 pi 助手能实时查任务库（事件/详情/统计），
// 而不是只依赖提问时附带的静态快照。只读：底层走 assistant_read.py，
// 该脚本只做 SELECT。随镜像发布（/app/pi-extensions/cms-tools.ts）。
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";

const SCRIPT = process.env.CMS_TOOLS_SCRIPT || "/app/scripts/assistant_read.py";

function run(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "python3",
      [SCRIPT, ...args],
      { timeout: 25_000, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          reject(new Error(String(stderr || err.message).slice(0, 500)));
        } else {
          resolve(stdout);
        }
      },
    );
  });
}

function textToolResult(text: string) {
  return { content: [{ type: "text", text: text.slice(0, 16_000) }], details: {} };
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "task_detail",
    label: "任务详情",
    description:
      "查询 cms-tg-ingest 指定任务的完整详情：状态、阶段、整理目标、完整事件流。用户问某个具体任务（如“#445 怎么了”）时用它获取实时数据。",
    parameters: Type.Object({
      task_id: Type.Number({ description: "任务 ID，例如 445" }),
    }),
    async execute(_toolCallId, params) {
      try {
        return textToolResult(await run(["task", String(params.task_id)]));
      } catch (err) {
        return textToolResult(`task_detail failed: ${String(err)}`);
      }
    },
  });

  pi.registerTool({
    name: "query_tasks",
    label: "查询任务列表",
    description:
      "按状态筛选最近任务列表（status 可选值：pending/running/succeeded/failed/needs_action/cancelled）。回答“现在有哪些任务”类问题时使用。",
    parameters: Type.Object({
      status: Type.Optional(Type.String({ description: "按状态过滤，留空返回全部最近任务" })),
      limit: Type.Optional(Type.Number({ description: "返回条数，默认 15，最大 200" })),
    }),
    async execute(_toolCallId, params) {
      const args = ["tasks"];
      if (params.status) args.push("--status", String(params.status));
      args.push("--limit", String(params.limit ?? 15));
      try {
        return textToolResult(await run(args));
      } catch (err) {
        return textToolResult(`query_tasks failed: ${String(err)}`);
      }
    },
  });

  pi.registerTool({
    name: "task_events",
    label: "任务事件流",
    description: "查询指定任务的事件流水（每个阶段的发生记录与失败原因），诊断任务历史时使用。",
    parameters: Type.Object({
      task_id: Type.Number({ description: "任务 ID" }),
      limit: Type.Optional(Type.Number({ description: "返回条数，默认 20" })),
    }),
    async execute(_toolCallId, params) {
      const args = ["events", String(params.task_id), "--limit", String(params.limit ?? 20)];
      try {
        return textToolResult(await run(args));
      } catch (err) {
        return textToolResult(`task_events failed: ${String(err)}`);
      }
    },
  });

  pi.registerTool({
    name: "system_stats",
    label: "系统统计",
    description:
      "最近 200 个任务的状态分布、needs_action 清单等系统级统计。用户问“系统整体怎么样/有什么积压”时使用。",
    parameters: Type.Object({}),
    async execute() {
      try {
        return textToolResult(await run(["stats"]));
      } catch (err) {
        return textToolResult(`system_stats failed: ${String(err)}`);
      }
    },
  });
}
