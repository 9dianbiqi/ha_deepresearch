<template>
  <main class="app-shell">
    <header class="app-topbar">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M5 12a7 7 0 1 0 14 0A7 7 0 0 0 5 12Z" />
            <path d="M12 8v4l3 2" />
            <path d="M4 20l3-3" />
          </svg>
        </div>
        <div>
          <p class="brand-title">HelloAgents DeepResearch</p>
          <p class="brand-subtitle">本地深度研究工作台</p>
        </div>
      </div>

      <div class="topbar-actions">
        <span class="connection-pill">
          <span class="live-dot"></span>
          {{ loading ? "正在接收流式事件" : "后端接口 localhost:8000" }}
        </span>
        <button
          class="icon-button"
          type="button"
          title="清空当前研究"
          aria-label="清空当前研究"
          :disabled="loading"
          @click="startNewResearch"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 12h14" />
            <path d="M12 5v14" />
          </svg>
        </button>
      </div>
    </header>

    <div class="research-workbench">
      <aside class="left-rail">
        <section class="surface-card research-card">
          <p class="eyebrow">新研究</p>
          <h1>把问题变成可追踪的研究流</h1>
          <p class="muted-copy">
            输入主题后，系统会拆分计划、检索证据、汇总任务并生成最终报告。
          </p>

          <form class="research-form" @submit.prevent="handleSubmit">
            <label class="field">
              <span>研究主题</span>
              <textarea
                v-model="form.topic"
                placeholder="例如：评估 2026 年开源多智能体框架的技术路线与采用风险"
                rows="5"
                required
              ></textarea>
            </label>

            <div class="field-row">
              <label class="field compact-field">
                <span>搜索引擎</span>
                <select v-model="form.searchApi">
                  <option value="">后端智能选择</option>
                  <option
                    v-for="option in searchOptions"
                    :key="option"
                    :value="option"
                  >
                    {{ option }}
                  </option>
                </select>
              </label>

              <button class="primary-button" type="submit" :disabled="loading">
                <svg
                  v-if="loading"
                  class="spinner"
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="12" r="8" />
                </svg>
                <svg v-else viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M5 12h14" />
                  <path d="m13 6 6 6-6 6" />
                </svg>
                {{ loading ? "研究进行中" : "开始研究" }}
              </button>
            </div>

            <label class="memory-toggle">
              <input v-model="form.useHistoryMemory" type="checkbox" />
              <span>自动参考相关历史（本次可关闭）</span>
            </label>

            <button
              v-if="loading"
              class="plain-button full-button"
              type="button"
              @click="cancelResearch"
            >
              取消当前研究
            </button>
          </form>

          <p v-if="error" class="error-banner">
            {{ error }}
          </p>
        </section>

        <section class="surface-card history-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">研究历史</p>
              <h2>浏览并继续</h2>
            </div>
            <button
              class="link-button"
              type="button"
              :disabled="historyLoading || loading"
              @click="loadHistory()"
            >
              {{ historyLoading ? "加载中" : "刷新" }}
            </button>
          </div>
          <div v-if="historyItems.length" class="history-list">
            <button
              v-for="item in historyItems"
              :key="item.run_id"
              class="history-item"
              type="button"
              :disabled="loading"
              @click="openHistory(item)"
            >
              <span class="history-item-title">{{ item.topic }}</span>
              <span class="history-item-meta">
                {{ formatHistoryDate(item.completed_at || item.started_at) }} ·
                {{ item.task_count }} 个任务
              </span>
              <span v-if="item.report_excerpt" class="history-item-excerpt">
                {{ item.report_excerpt }}
              </span>
            </button>
          </div>
          <p v-else class="empty-copy">
            {{ historyError || "完成一次研究后，历史记录会出现在这里。" }}
          </p>
          <button
            v-if="historyCursor"
            class="plain-button full-button history-more"
            type="button"
            :disabled="historyLoading || loading"
            @click="loadHistory(historyCursor)"
          >
            加载更多
          </button>
        </section>

        <section class="surface-card memory-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">用户记忆</p>
              <h2>确认后才会使用</h2>
            </div>
            <button
              class="link-button"
              type="button"
              :disabled="memoryLoading || loading"
              @click="loadMemories()"
            >
              {{ memoryLoading ? "加载中" : "刷新" }}
            </button>
          </div>
          <form class="memory-form" @submit.prevent="saveMemoryCandidate">
            <label class="field">
              <span>记住一条偏好或事实</span>
              <textarea
                v-model="memoryDraft"
                rows="2"
                maxlength="240"
                placeholder="例如：我偏好用简洁的中文报告"
              ></textarea>
            </label>
            <div class="field-row">
              <select v-model="memoryKind" aria-label="记忆类型">
                <option value="preference">偏好</option>
                <option value="fact">事实</option>
              </select>
              <button class="plain-button" type="submit" :disabled="memoryLoading || !memoryDraft.trim()">
                提交待确认
              </button>
            </div>
          </form>
          <p v-if="memoryError" class="error-banner">{{ memoryError }}</p>
          <div v-if="userMemories.length" class="memory-list">
            <article v-for="memory in userMemories" :key="memory.memory_id" class="memory-item">
              <div class="memory-item-main">
                <span class="status-tag" :class="memory.status === 'confirmed' ? 'completed' : 'pending'">
                  {{ memory.status === "confirmed" ? "已确认" : "待确认" }}
                </span>
                <span class="memory-item-text">{{ memory.text }}</span>
              </div>
              <div class="memory-item-actions">
                <button
                  v-if="memory.status === 'pending'"
                  class="link-button"
                  type="button"
                  :disabled="memoryLoading || loading"
                  @click="confirmUserMemory(memory)"
                >
                  确认
                </button>
                <button
                  class="link-button danger-link"
                  type="button"
                  :disabled="memoryLoading || loading"
                  @click="removeUserMemory(memory)"
                >
                  删除
                </button>
              </div>
            </article>
          </div>
          <p v-else-if="!memoryLoading" class="empty-copy">
            还没有用户记忆；研究报告不会自动写入这里。
          </p>
        </section>

        <section class="surface-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">流程预览</p>
              <h2>计划到报告</h2>
            </div>
            <span class="status-tag in_progress">流式更新</span>
          </div>
          <div class="flow-strip">
            <div class="flow-step">
              <strong>计划</strong>
              <span>生成任务</span>
            </div>
            <div class="flow-step">
              <strong>检索</strong>
              <span>搜索来源</span>
            </div>
            <div class="flow-step">
              <strong>摘要</strong>
              <span>任务结论</span>
            </div>
            <div class="flow-step">
              <strong>报告</strong>
              <span>整合输出</span>
            </div>
          </div>
        </section>

        <section class="surface-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">当前进度</p>
              <h2>{{ completedTasks }} / {{ totalTasks || 0 }} 任务完成</h2>
            </div>
            <span class="status-tag" :class="loading ? 'in_progress' : 'pending'">
              {{ loading ? "进行中" : "待开始" }}
            </span>
          </div>

          <div class="progress-track" aria-label="研究进度">
            <span :style="{ width: `${progressPercent}%` }"></span>
          </div>

          <div class="metric-grid">
            <div class="metric">
              <strong>{{ totalSources }}</strong>
              <span>来源</span>
            </div>
            <div class="metric">
              <strong>{{ noteCount }}</strong>
              <span>笔记</span>
            </div>
            <div class="metric">
              <strong>{{ progressLogs.length }}</strong>
              <span>事件</span>
            </div>
            <div class="metric">
              <strong>{{ historyRecallCount }}</strong>
              <span>相关历史</span>
            </div>
          </div>
          <p v-if="streamTelemetry" class="telemetry-copy">
            {{ streamTelemetryLabel }}
          </p>
        </section>

        <section class="surface-card task-list-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">任务清单</p>
              <h2>研究计划</h2>
            </div>
          </div>

          <div v-if="todoTasks.length" class="task-list">
            <button
              v-for="task in todoTasks"
              :key="task.id"
              class="task-item"
              :class="{ active: task.id === activeTaskId }"
              type="button"
              @click="activeTaskId = task.id"
            >
              <span class="task-row">
                <span class="task-title">{{ task.title }}</span>
                <span class="status-tag" :class="task.status">
                  {{ formatTaskStatus(task.status) }}
                </span>
              </span>
              <span class="task-intent">{{ task.intent }}</span>
            </button>
          </div>

          <p v-else class="empty-copy">
            提交主题后，任务计划会出现在这里。
          </p>
        </section>
      </aside>

      <section class="center-pane">
        <section v-if="!hasWorkspaceData" class="surface-card empty-state">
          <p class="eyebrow">准备开始</p>
          <h1>研究结果会在这里实时展开</h1>
          <p>
            任务、来源、摘要和最终报告会随着后端 SSE 事件逐步进入工作台。
          </p>
          <div class="empty-grid">
            <div>
              <strong>任务详情</strong>
              <span>查看当前子任务目标和查询词</span>
            </div>
            <div>
              <strong>最新来源</strong>
              <span>保留标题、链接和摘要片段</span>
            </div>
            <div>
              <strong>实时总结</strong>
              <span>Markdown 内容流式写入</span>
            </div>
            <div>
              <strong>最终报告</strong>
              <span>研究完成后集中阅读和下载</span>
            </div>
          </div>
        </section>

        <template v-else>
          <header class="pane-toolbar">
            <div>
              <p class="eyebrow">活动任务</p>
              <h1>{{ currentTaskTitle || "等待任务规划" }}</h1>
              <p v-if="currentTaskIntent" class="muted-copy">
                {{ currentTaskIntent }}
              </p>
            </div>
            <div class="toolbar-actions">
              <span class="status-tag" :class="currentTask?.status || 'pending'">
                {{ formatTaskStatus(currentTask?.status || "pending") }}
              </span>
              <button
                class="plain-button"
                type="button"
                @click="logsCollapsed = !logsCollapsed"
              >
                {{ logsCollapsed ? "显示事件" : "隐藏事件" }}
              </button>
            </div>
          </header>

          <section v-if="currentTask" class="surface-card task-overview">
            <div class="task-query">
              <p class="eyebrow">查询</p>
              <h2>{{ currentTaskQuery || currentTopic || form.topic }}</h2>
              <p v-if="currentTaskNotePath" class="note-path">
                <span>笔记路径</span>
                <button
                  class="link-button"
                  type="button"
                  @click="copyNotePath(currentTaskNotePath)"
                >
                  复制
                </button>
                <span class="path-text">{{ currentTaskNotePath }}</span>
              </p>
            </div>

            <div v-if="currentTask.notices.length" class="notice-list">
              <p class="eyebrow">系统提示</p>
              <ul>
                <li
                  v-for="(notice, idx) in currentTask.notices"
                  :key="`${notice}-${idx}`"
                >
                  {{ notice }}
                </li>
              </ul>
            </div>
          </section>

          <section
            class="surface-card"
            :class="{ 'block-highlight': sourcesHighlight }"
          >
            <div class="section-heading">
              <div>
                <p class="eyebrow">最新来源</p>
                <h2>检索证据</h2>
              </div>
              <span class="status-tag pending">
                {{ currentTaskSources.length }} 条
              </span>
            </div>

            <div v-if="currentTaskSources.length" class="source-list">
              <article
                v-for="(item, index) in currentTaskSources"
                :key="`${item.title}-${index}`"
                class="source-card"
              >
                <a
                  :href="item.url || '#'"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {{ item.title || item.url || `来源 ${index + 1}` }}
                </a>
                <p v-if="item.snippet">{{ item.snippet }}</p>
                <p v-else-if="item.raw">{{ item.raw }}</p>
              </article>
            </div>
            <p v-else class="empty-copy">暂无可用来源。</p>
          </section>

          <section
            class="surface-card"
            :class="{ 'block-highlight': summaryHighlight }"
          >
            <div class="section-heading">
              <div>
                <p class="eyebrow">任务总结</p>
                <h2>实时 Markdown 预览</h2>
              </div>
              <span class="status-tag" :class="loading ? 'in_progress' : 'pending'">
                {{ loading ? "流式写入" : "已暂停" }}
              </span>
            </div>

            <div
              v-if="renderedTaskSummary"
              class="markdown-body"
              v-html="renderedTaskSummary"
            ></div>
            <p v-else class="empty-copy">暂无可用信息。</p>
          </section>

          <section
            v-if="reportMarkdown"
            class="surface-card report-card"
            :class="{ 'block-highlight': reportHighlight }"
          >
            <div class="section-heading">
              <div>
                <p class="eyebrow">最终报告</p>
                <h2>整合输出</h2>
              </div>
              <span class="status-tag completed">已生成</span>
            </div>
            <div class="markdown-body report-body" v-html="renderedReport"></div>
          </section>
        </template>
      </section>

      <aside class="inspector">
        <section class="surface-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">状态</p>
              <h2>{{ statusLabel }}</h2>
            </div>
            <span class="status-tag" :class="statusTone">
              {{ loading ? "Live" : "Idle" }}
            </span>
          </div>
          <p class="muted-copy">
            {{ currentTopic || form.topic || "等待研究主题" }}
          </p>
        </section>

        <section class="surface-card event-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">事件时间线</p>
              <h2>流式状态</h2>
            </div>
            <span class="status-tag pending">{{ progressLogs.length }}</span>
          </div>

          <div v-if="!logsCollapsed && visibleLogs.length" class="timeline">
            <div
              v-for="(log, index) in visibleLogs"
              :key="`${log}-${index}`"
              class="event-row"
              :class="{ live: loading && index === visibleLogs.length - 1 }"
            >
              <span class="event-dot"></span>
              <p>{{ log }}</p>
            </div>
          </div>
          <p v-else class="empty-copy">
            {{ logsCollapsed ? "事件已隐藏。" : "暂无事件。" }}
          </p>
        </section>

        <section class="surface-card tool-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">工具调用</p>
              <h2>证据层</h2>
            </div>
            <span class="status-tag pending">{{ visibleToolCalls.length }}</span>
          </div>

          <div v-if="visibleToolCalls.length" class="tool-list">
            <article
              v-for="entry in visibleToolCalls"
              :key="`${entry.eventId}-${entry.timestamp}`"
              class="tool-entry"
            >
              <div class="task-row">
                <strong>#{{ entry.eventId }} {{ entry.tool }}</strong>
                <span v-if="entry.noteId" class="status-tag completed">
                  {{ entry.noteId }}
                </span>
              </div>
              <p class="tool-agent">{{ entry.agent }}</p>
              <pre>{{ formatToolParameters(entry.parameters) }}</pre>
              <pre v-if="entry.result">{{ formatToolResult(entry.result) }}</pre>
            </article>
          </div>
          <p v-else class="empty-copy">暂无工具调用。</p>
        </section>

        <section v-if="githubIntelligence" class="surface-card evidence-drawer">
          <div class="section-heading">
            <div>
              <p class="eyebrow">GitHub Evidence Drawer</p>
              <h2>证据与覆盖</h2>
            </div>
            <span class="status-tag completed">
              {{ githubIntelligence.evidence?.length || 0 }} 条
            </span>
          </div>
          <p class="muted-copy">
            覆盖率 {{ formatCoverage(githubIntelligence.coverage?.coverage_score) }} ·
            {{ githubIntelligence.snapshots?.length || 0 }} 个固定快照
          </p>
          <div v-if="githubIntelligence.coverage?.missing_dimensions?.length" class="notice-list">
            <p class="eyebrow">待补证据</p>
            <p>{{ githubIntelligence.coverage.missing_dimensions.join("、") }}</p>
          </div>
          <div v-if="githubIntelligence.claims?.length" class="claim-list">
            <article v-for="claim in githubIntelligence.claims.slice(0, 4)" :key="String(claim.claim_id)" class="claim-item">
              <strong>{{ claim.category || "claim" }}</strong>
              <span>{{ claim.statement || "" }}</span>
            </article>
          </div>
          <div v-if="(githubIntelligence.snapshots?.length || 0) > 1" class="comparison-list">
            <p class="eyebrow">Comparison View</p>
            <article v-for="snapshot in githubIntelligence.snapshots" :key="String(snapshot.snapshot_id)" class="comparison-row">
              <strong>{{ snapshot.repository || "repository" }}</strong>
              <span>{{ snapshot.commit_sha || "未取得 SHA" }}</span>
              <span>{{ snapshot.collection_status || "partial" }}</span>
            </article>
          </div>
        </section>

        <section v-if="artifactManifest.length" class="surface-card artifacts-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Artifacts Panel</p>
              <h2>报告产物</h2>
            </div>
            <span class="status-tag pending">{{ artifactManifest.length }}</span>
          </div>
          <div class="artifact-list">
            <article v-for="artifact in artifactManifest" :key="artifact.artifact_id" class="artifact-row">
              <div>
                <strong>{{ artifact.title }}</strong>
                <span>{{ artifact.mime_type }}</span>
              </div>
              <button class="link-button" type="button" @click="downloadArtifact(artifact)">下载</button>
            </article>
          </div>
        </section>

        <section class="surface-card report-actions-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">报告操作</p>
              <h2>下载与追问</h2>
            </div>
          </div>

          <template v-if="(reportMarkdown || resumableRunId) && !loading">
            <div v-if="!continueMode" class="report-actions">
              <button class="plain-button" type="button" @click="downloadReport">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 4v11" />
                  <path d="m7 10 5 5 5-5" />
                  <path d="M5 20h14" />
                </svg>
                下载报告
              </button>
              <button
                class="primary-button"
                type="button"
                :disabled="!resumableRunId"
                @click="continueMode = true"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M5 12h14" />
                  <path d="m13 6 6 6-6 6" />
                </svg>
                继续追问
              </button>
            </div>

            <form v-else class="continue-form" @submit.prevent="handleContinue">
              <label class="field">
                <span>追问主题</span>
                <textarea
                  v-model="form.followupTopic"
                  placeholder="基于刚才的研究结果，进一步追问..."
                  rows="4"
                  required
                ></textarea>
              </label>
              <label class="memory-toggle">
                <input v-model="form.useHistoryMemory" type="checkbox" />
                <span>同时参考其他相关历史（精确续跑仍保留）</span>
              </label>
              <div class="report-actions">
                <button class="primary-button" type="submit" :disabled="loading">
                  开始追问
                </button>
                <button
                  class="plain-button"
                  type="button"
                  @click="continueMode = false"
                >
                  取消
                </button>
              </div>
            </form>
          </template>

          <p v-else class="empty-copy">
            最终报告生成后，可在这里下载或继续追问。
          </p>
        </section>
      </aside>
    </div>
  </main>
</template>

<script lang="ts" setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { marked } from "marked";

import {
  confirmMemory,
  createMemoryCandidate,
  deleteMemory,
  listMemories,
  runContinueStream,
  runResearchStream,
  getRunRecord,
  listHistory,
  type GithubArtifact,
  type GithubIntelligence,
  type ContinueRequest,
  type HistoryItem,
  type ResearchStreamEvent,
  type StreamTelemetry,
  type UserMemory,
} from "./services/api";

marked.setOptions({
  breaks: true,
  gfm: true,
});

interface SourceItem {
  title: string;
  url: string;
  snippet: string;
  raw: string;
}

interface ToolCallLog {
  eventId: number;
  agent: string;
  tool: string;
  parameters: Record<string, unknown>;
  result: string;
  noteId: string | null;
  notePath: string | null;
  timestamp: number;
}

interface TodoTaskView {
  id: number;
  title: string;
  intent: string;
  query: string;
  status: string;
  summary: string;
  sourcesSummary: string;
  sourceItems: SourceItem[];
  notices: string[];
  noteId: string | null;
  notePath: string | null;
  toolCalls: ToolCallLog[];
}

const form = reactive({
  topic: "",
  followupTopic: "",
  searchApi: "",
  useHistoryMemory: true,
});

const loading = ref(false);
const error = ref("");
const progressLogs = ref<string[]>([]);
const logsCollapsed = ref(false);
const todoTasks = ref<TodoTaskView[]>([]);
const activeTaskId = ref<number | null>(null);
const reportMarkdown = ref("");
const currentRunId = ref<string | null>(null);
const resumableRunId = ref<string | null>(null);
const streamTelemetry = ref<StreamTelemetry | null>(null);
const currentTopic = ref("");
const historyItems = ref<HistoryItem[]>([]);
const historyCursor = ref<string | null>(null);
const historyLoading = ref(false);
const historyError = ref("");
const historyRecallCount = ref(0);
const userMemories = ref<UserMemory[]>([]);
const memoryDraft = ref("");
const memoryKind = ref<"preference" | "fact">("preference");
const memoryLoading = ref(false);
const memoryError = ref("");
const continueMode = ref(false);
const summaryHighlight = ref(false);
const sourcesHighlight = ref(false);
const reportHighlight = ref(false);
const toolHighlight = ref(false);
const githubIntelligence = ref<GithubIntelligence | null>(null);
const artifactManifest = ref<GithubArtifact[]>([]);

let currentController: AbortController | null = null;
let pulseRaf = 0;
let pulseTimer = 0;

const searchOptions = [
  "advanced",
  "duckduckgo",
  "tavily",
  "perplexity",
  "searxng",
];

const TASK_STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  in_progress: "进行中",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
};

const totalTasks = computed(() => todoTasks.value.length);
const completedTasks = computed(() =>
  todoTasks.value.filter((task) => task.status === "completed").length
);
const progressPercent = computed(() =>
  totalTasks.value
    ? Math.round((completedTasks.value / totalTasks.value) * 100)
    : 0
);
const hasWorkspaceData = computed(
  () =>
    todoTasks.value.length > 0 ||
    progressLogs.value.length > 0 ||
    Boolean(reportMarkdown.value)
);
const currentTask = computed(() => {
  if (activeTaskId.value !== null) {
    return todoTasks.value.find((task) => task.id === activeTaskId.value) ?? null;
  }
  return todoTasks.value[0] ?? null;
});
const currentTaskSources = computed(() => currentTask.value?.sourceItems ?? []);
const currentTaskTitle = computed(() => currentTask.value?.title ?? "");
const currentTaskIntent = computed(() => currentTask.value?.intent ?? "");
const currentTaskQuery = computed(() => currentTask.value?.query ?? "");
const currentTaskNotePath = computed(() => currentTask.value?.notePath ?? "");
const visibleLogs = computed(() => progressLogs.value.slice(-9));
const visibleToolCalls = computed(() => {
  const current = currentTask.value?.toolCalls ?? [];
  if (current.length) {
    return current.slice(-4);
  }
  return todoTasks.value.flatMap((task) => task.toolCalls).slice(-4);
});
const totalSources = computed(() =>
  todoTasks.value.reduce((sum, task) => sum + task.sourceItems.length, 0)
);
const noteCount = computed(
  () => todoTasks.value.filter((task) => task.noteId || task.notePath).length
);
const statusLabel = computed(() => {
  if (loading.value) {
    return "研究进行中";
  }
  if (error.value) {
    return "需要处理";
  }
  if (reportMarkdown.value) {
    return "报告已生成";
  }
  if (hasWorkspaceData.value) {
    return "等待下一步";
  }
  return "待开始";
});
const statusTone = computed(() => {
  if (loading.value) return "in_progress";
  if (error.value) return "failed";
  if (reportMarkdown.value) return "completed";
  return "pending";
});
const streamTelemetryLabel = computed(() => {
  const telemetry = streamTelemetry.value;
  if (!telemetry) return "";
  const duration = `${Math.round(telemetry.duration_ms)} ms`;
  const firstEvent = telemetry.first_event_latency_ms === null
    ? "--"
    : `${Math.round(telemetry.first_event_latency_ms)} ms`;
  const state = telemetry.stream_completed ? "正常结束" : "中途结束";
  return `SSE ${state} · ${telemetry.event_count} 事件 · 首事件 ${firstEvent} · 总时长 ${duration}`;
});

const sanitizeHtml = (html: string): string =>
  html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/\bon\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\bon\w+\s*=\s*'[^']*'/gi, "")
    .replace(/<iframe[\s\S]*?<\/iframe>/gi, "")
    .replace(/<object[\s\S]*?<\/object>/gi, "")
    .replace(/<embed[\s\S]*?>/gi, "");

const renderedTaskSummary = computed(() => {
  const raw = currentTask.value?.summary;
  if (!raw) return "";
  return sanitizeHtml(marked.parse(raw) as string);
});

const renderedReport = computed(() => {
  if (!reportMarkdown.value) return "";
  return sanitizeHtml(marked.parse(reportMarkdown.value) as string);
});

function formatTaskStatus(status: string): string {
  return TASK_STATUS_LABEL[status] ?? status;
}

function formatCoverage(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 100)}%`
    : "0%";
}

function setGithubIntelligence(value: unknown): void {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    githubIntelligence.value = null;
    artifactManifest.value = [];
    return;
  }
  const bundle = value as GithubIntelligence;
  githubIntelligence.value = bundle;
  const rawArtifacts = Array.isArray(bundle.artifacts)
    ? bundle.artifacts
    : bundle.artifact_manifest?.artifacts;
  artifactManifest.value = Array.isArray(rawArtifacts)
    ? rawArtifacts.filter(
        (artifact): artifact is GithubArtifact =>
          Boolean(artifact && typeof artifact.artifact_id === "string"),
      )
    : [];
}

function downloadArtifact(artifact: GithubArtifact): void {
  if (typeof artifact.content !== "string") return;
  const blob = new Blob([artifact.content], { type: artifact.mime_type || "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = artifact.path.split("/").pop() || `${artifact.artifact_id}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function pulse(flag: { value: boolean }) {
  cancelAnimationFrame(pulseRaf);
  clearTimeout(pulseTimer);
  flag.value = false;
  pulseRaf = requestAnimationFrame(() => {
    flag.value = true;
    pulseTimer = window.setTimeout(() => {
      flag.value = false;
    }, 900);
  });
}

function clearAnimations() {
  cancelAnimationFrame(pulseRaf);
  clearTimeout(pulseTimer);
  pulseRaf = 0;
  pulseTimer = 0;
}

function formatHistoryDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

async function loadHistory(cursor?: string | null): Promise<void> {
  if (historyLoading.value) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    const page = await listHistory(20, cursor || undefined);
    historyItems.value = cursor
      ? [...historyItems.value, ...page.items]
      : page.items;
    historyCursor.value = page.next_cursor;
  } catch (err) {
    historyError.value = err instanceof Error ? err.message : "无法加载研究历史";
  } finally {
    historyLoading.value = false;
  }
}

async function loadMemories(): Promise<void> {
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    const page = await listMemories("default", true, 50);
    userMemories.value = page.items;
  } catch (err) {
    memoryError.value = err instanceof Error ? err.message : "无法加载用户记忆";
  } finally {
    memoryLoading.value = false;
  }
}

async function saveMemoryCandidate(): Promise<void> {
  const text = memoryDraft.value.trim();
  if (!text || memoryLoading.value) return;
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    await createMemoryCandidate(text, memoryKind.value, "default");
    memoryDraft.value = "";
    await loadMemories();
  } catch (err) {
    memoryError.value = err instanceof Error ? err.message : "无法保存记忆候选";
  } finally {
    memoryLoading.value = false;
  }
}

async function confirmUserMemory(memory: UserMemory): Promise<void> {
  if (memoryLoading.value) return;
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    await confirmMemory(memory.memory_id, memory.scope);
    await loadMemories();
  } catch (err) {
    memoryError.value = err instanceof Error ? err.message : "无法确认记忆";
  } finally {
    memoryLoading.value = false;
  }
}

async function removeUserMemory(memory: UserMemory): Promise<void> {
  if (memoryLoading.value) return;
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    await deleteMemory(memory.memory_id, memory.scope);
    await loadMemories();
  } catch (err) {
    memoryError.value = err instanceof Error ? err.message : "无法删除记忆";
  } finally {
    memoryLoading.value = false;
  }
}

async function openHistory(item: HistoryItem): Promise<void> {
  if (loading.value || historyLoading.value) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    const record = await getRunRecord(item.run_id);
    const output = ensureRecord(record.output);
    resetWorkflowState();
    currentRunId.value = record.run_id;
    resumableRunId.value =
      record.resumable === false
        ? record.last_resumable_parent ?? null
        : record.run_id;
    currentTopic.value = record.topic || item.topic;
    form.topic = "";
    form.followupTopic = "";
    todoTasks.value = normalizeTasks(output.todo_items);
    if (todoTasks.value.length) {
      activeTaskId.value = todoTasks.value[0].id;
    }
    reportMarkdown.value =
      extractOptionalString(output.report_markdown) ??
      extractOptionalString(output.running_summary) ??
      "";
    setGithubIntelligence(output.github_intelligence);
    progressLogs.value = [`已恢复历史研究：${currentTopic.value}`];
    historyRecallCount.value = 0;
    try {
      window.localStorage.setItem("helloagents:last-run-id", record.run_id);
    } catch {
      // Local storage is optional; the history API remains authoritative.
    }
  } catch (err) {
    historyError.value = err instanceof Error ? err.message : "无法加载研究记录";
  } finally {
    historyLoading.value = false;
  }
}

async function hydrateRunArtifacts(runId: string | null): Promise<void> {
  if (!runId) return;
  try {
    const record = await getRunRecord(runId);
    const output = ensureRecord(record.output);
    setGithubIntelligence(output.github_intelligence);
  } catch {
    // The SSE terminal event remains authoritative if the record is not yet readable.
  }
}

function parseSources(raw: string): SourceItem[] {
  if (!raw) {
    return [];
  }

  const items: SourceItem[] = [];
  const lines = raw.split("\n");
  let current: SourceItem | null = null;

  const truncate = (value: string, max = 360) => {
    const trimmed = value.trim();
    return trimmed.length > max ? `${trimmed.slice(0, max)}...` : trimmed;
  };

  const flush = () => {
    if (!current) return;

    const normalized: SourceItem = {
      title: current.title?.trim() || "",
      url: current.url?.trim() || "",
      snippet: current.snippet ? truncate(current.snippet) : "",
      raw: current.raw ? truncate(current.raw, 420) : "",
    };

    if (
      normalized.title ||
      normalized.url ||
      normalized.snippet ||
      normalized.raw
    ) {
      if (!normalized.title && normalized.url) {
        normalized.title = normalized.url;
      }
      items.push(normalized);
    }
    current = null;
  };

  const ensureCurrent = () => {
    if (!current) {
      current = { title: "", url: "", snippet: "", raw: "" };
    }
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    if (/^\*/.test(trimmed) && trimmed.includes(" : ")) {
      flush();
      const withoutBullet = trimmed.replace(/^\*\s*/, "");
      const [titlePart, urlPart] = withoutBullet.split(" : ");
      current = {
        title: titlePart?.trim() || "",
        url: urlPart?.trim() || "",
        snippet: "",
        raw: "",
      };
      continue;
    }

    if (/^(Source|信息来源)\s*:/.test(trimmed)) {
      flush();
      const [, titlePart = ""] = trimmed.split(/:\s*(.+)/);
      current = {
        title: titlePart.trim(),
        url: "",
        snippet: "",
        raw: "",
      };
      continue;
    }

    if (/^URL\s*:/.test(trimmed)) {
      ensureCurrent();
      const [, urlPart = ""] = trimmed.split(/:\s*(.+)/);
      current!.url = urlPart.trim();
      continue;
    }

    if (/^(Most relevant content from source|信息内容)\s*:/.test(trimmed)) {
      ensureCurrent();
      const [, contentPart = ""] = trimmed.split(/:\s*(.+)/);
      current!.snippet = contentPart.trim();
      continue;
    }

    if (/^(Full source content limited to|信息内容限制为)\s*:/.test(trimmed)) {
      ensureCurrent();
      const [, rawPart = ""] = trimmed.split(/:\s*(.+)/);
      current!.raw = rawPart.trim();
      continue;
    }

    if (/^https?:\/\//.test(trimmed)) {
      ensureCurrent();
      if (!current!.url) {
        current!.url = trimmed;
        continue;
      }
    }

    ensureCurrent();
    current!.raw = current!.raw ? `${current!.raw}\n${trimmed}` : trimmed;
  }

  flush();
  return items;
}

function extractOptionalString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function ensureRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function createTaskView(item: Record<string, unknown>, index: number): TodoTaskView {
  const rawId =
    typeof item.id === "number"
      ? item.id
      : typeof item.id === "string"
      ? Number(item.id)
      : index + 1;
  const id = Number.isFinite(rawId) ? Number(rawId) : index + 1;
  const sourcesSummary = extractOptionalString(item.sources_summary) ?? "";

  return {
    id,
    title: extractOptionalString(item.title) ?? `任务 ${id}`,
    intent: extractOptionalString(item.intent) ?? "探索与主题相关的关键信息",
    query: extractOptionalString(item.query) ?? (currentTopic.value || form.topic).trim(),
    status: extractOptionalString(item.status) ?? "pending",
    summary: extractOptionalString(item.summary) ?? "",
    sourcesSummary,
    sourceItems: sourcesSummary ? parseSources(sourcesSummary) : [],
    notices: [],
    noteId: extractOptionalString(item.note_id),
    notePath: extractOptionalString(item.note_path),
    toolCalls: [],
  };
}

function normalizeTasks(value: unknown): TodoTaskView[] {
  const tasks = Array.isArray(value) ? value : [];
  return tasks.map((item, index) =>
    createTaskView(ensureRecord(item), index)
  );
}

function applyNoteMetadata(
  task: TodoTaskView,
  payload: Record<string, unknown>
): void {
  const noteId = extractOptionalString(payload.note_id);
  if (noteId) {
    task.noteId = noteId;
  }
  const notePath = extractOptionalString(payload.note_path);
  if (notePath) {
    task.notePath = notePath;
  }
}

function upsertTaskMetadata(
  task: TodoTaskView,
  payload: Record<string, unknown>
) {
  if (typeof payload.title === "string" && payload.title.trim()) {
    task.title = payload.title.trim();
  }
  if (typeof payload.intent === "string" && payload.intent.trim()) {
    task.intent = payload.intent.trim();
  }
  if (typeof payload.query === "string" && payload.query.trim()) {
    task.query = payload.query.trim();
  }
}

function findTask(taskId: unknown): TodoTaskView | undefined {
  const numeric =
    typeof taskId === "number"
      ? taskId
      : typeof taskId === "string"
      ? Number(taskId)
      : NaN;
  if (Number.isNaN(numeric)) {
    return undefined;
  }
  return todoTasks.value.find((task) => task.id === numeric);
}

function formatToolParameters(parameters: Record<string, unknown>): string {
  try {
    return JSON.stringify(parameters, null, 2);
  } catch (err) {
    console.warn("无法格式化工具参数", err, parameters);
    return Object.entries(parameters)
      .map(([key, value]) => `${key}: ${String(value)}`)
      .join("\n");
  }
}

function formatToolResult(result: string): string {
  const trimmed = result.trim();
  const limit = 900;
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}...` : trimmed;
}

async function copyNotePath(path: string | null | undefined) {
  if (!path) return;

  try {
    await navigator.clipboard.writeText(path);
    progressLogs.value.push(`已复制笔记路径：${path}`);
  } catch (err) {
    console.warn("无法直接复制到剪贴板", err);
    window.prompt("复制以下笔记路径", path);
    progressLogs.value.push("请手动复制笔记路径");
  }
}

function resetWorkflowState(
  options: {
    preserveRunId?: boolean;
    preserveResumableRunId?: boolean;
  } = {},
) {
  clearAnimations();
  todoTasks.value = [];
  activeTaskId.value = null;
  reportMarkdown.value = "";
  githubIntelligence.value = null;
  artifactManifest.value = [];
  streamTelemetry.value = null;
  progressLogs.value = [];
  summaryHighlight.value = false;
  sourcesHighlight.value = false;
  reportHighlight.value = false;
  toolHighlight.value = false;
  historyRecallCount.value = 0;
  logsCollapsed.value = false;
  continueMode.value = false;
  error.value = "";
  if (!options.preserveRunId) {
    currentRunId.value = null;
    currentTopic.value = "";
  }
  if (!options.preserveResumableRunId) {
    resumableRunId.value = null;
  }
}

function handleStreamEvent(event: ResearchStreamEvent) {
  const payload = event as Record<string, unknown>;

  if (event.stream_telemetry) {
    streamTelemetry.value = event.stream_telemetry;
  }

  if (typeof payload.run_id === "string" && payload.run_id.trim()) {
    currentRunId.value = payload.run_id.trim();
  }

  if (event.type === "history_recalled") {
    const count =
      typeof payload.match_count === "number" &&
      Number.isFinite(payload.match_count)
        ? Math.max(0, Math.floor(payload.match_count))
        : 0;
    historyRecallCount.value = count;
    progressLogs.value.push(
      count ? `已参考 ${count} 条相关历史研究` : "未找到高相关历史研究",
    );
    return;
  }

  if (event.type === "github_evidence") {
    progressLogs.value.push(
      `已冻结 GitHub 证据：${payload.evidence_count || 0} 条，${payload.claim_count || 0} 个结论`,
    );
    return;
  }

  if (event.type === "coverage_update") {
    const current = githubIntelligence.value ?? {};
    const coverageScore =
      typeof payload.coverage_score === "number" ? payload.coverage_score : 0;
    githubIntelligence.value = {
      ...current,
      coverage: {
        ...(current.coverage || {}),
        coverage_score: coverageScore,
        covered_dimensions: Array.isArray(payload.covered_dimensions)
          ? payload.covered_dimensions.filter((item): item is string => typeof item === "string")
          : [],
        missing_dimensions: Array.isArray(payload.missing_dimensions)
          ? payload.missing_dimensions.filter((item): item is string => typeof item === "string")
          : [],
        gap_queries: Array.isArray(payload.gap_queries)
          ? payload.gap_queries.filter((item): item is string => typeof item === "string")
          : [],
        allow_report: payload.allow_report === true,
      },
    };
    return;
  }

  if (event.type === "artifact_ready") {
    progressLogs.value.push(`已生成报告产物：${payload.title || payload.path || "artifact"}`);
    return;
  }

  if (event.type === "done") {
    if (event.resumable !== false && currentRunId.value) {
      resumableRunId.value = currentRunId.value;
    } else if (typeof event.last_resumable_parent === "string") {
      resumableRunId.value = event.last_resumable_parent;
    }
    void hydrateRunArtifacts(currentRunId.value);
    try {
      if (currentRunId.value) {
        window.localStorage.setItem("helloagents:last-run-id", currentRunId.value);
      }
    } catch {
      // Local storage is optional.
    }
    void loadHistory();
    return;
  }

  if (event.type === "status") {
    const message =
      typeof payload.message === "string" && payload.message.trim()
        ? payload.message.trim()
        : "流程状态更新";
    progressLogs.value.push(message);

    const task = findTask(payload.task_id);
    if (task) {
      task.notices.push(message);
      applyNoteMetadata(task, payload);
    }
    return;
  }

  if (event.type === "todo_list") {
    todoTasks.value = normalizeTasks(payload.tasks);
    if (todoTasks.value.length) {
      activeTaskId.value = todoTasks.value[0].id;
      progressLogs.value.push("已生成任务清单");
    } else {
      progressLogs.value.push("未生成任务清单，使用默认任务继续");
    }
    return;
  }

  if (event.type === "task_status") {
    const task = findTask(payload.task_id);
    if (!task) return;

    upsertTaskMetadata(task, payload);
    applyNoteMetadata(task, payload);
    const status = extractOptionalString(payload.status) ?? task.status;
    task.status = status;

    if (status === "in_progress") {
      task.summary = "";
      task.sourcesSummary = "";
      task.sourceItems = [];
      task.notices = [];
      activeTaskId.value = task.id;
      progressLogs.value.push(`开始执行任务：${task.title}`);
    } else if (status === "completed") {
      const summary = extractOptionalString(payload.summary);
      if (summary) {
        task.summary = summary;
      }
      const sourcesSummary = extractOptionalString(payload.sources_summary);
      if (sourcesSummary) {
        task.sourcesSummary = sourcesSummary;
        task.sourceItems = parseSources(sourcesSummary);
      }
      progressLogs.value.push(`完成任务：${task.title}`);
      if (activeTaskId.value === task.id) {
        pulse(summaryHighlight);
        pulse(sourcesHighlight);
      }
    } else if (status === "skipped") {
      progressLogs.value.push(`任务跳过：${task.title}`);
    } else if (status === "failed") {
      progressLogs.value.push(`任务失败：${task.title}`);
    }
    return;
  }

  if (event.type === "sources") {
    const task = findTask(payload.task_id);
    if (!task) return;

    const latestText = [
      payload.latest_sources,
      payload.sources_summary,
      payload.raw_context,
    ]
      .map((value) => (typeof value === "string" ? value.trim() : ""))
      .find(Boolean);

    if (latestText) {
      task.sourcesSummary = latestText;
      task.sourceItems = parseSources(latestText);
      progressLogs.value.push(`已更新任务来源：${task.title}`);
      if (activeTaskId.value === task.id) {
        pulse(sourcesHighlight);
      }
    }

    if (typeof payload.backend === "string") {
      progressLogs.value.push(`当前使用搜索后端：${payload.backend}`);
    }
    applyNoteMetadata(task, payload);
    return;
  }

  if (event.type === "task_summary_chunk") {
    const task = findTask(payload.task_id);
    if (!task) return;

    const chunk = typeof payload.content === "string" ? payload.content : "";
    task.summary += chunk;
    applyNoteMetadata(task, payload);
    if (activeTaskId.value === task.id) {
      pulse(summaryHighlight);
    }
    return;
  }

  if (event.type === "task_retry") {
    const task = findTask(payload.task_id);
    const refined = extractOptionalString(payload.refined_query) ?? "";
    const reason = extractOptionalString(payload.reason) ?? "";
    progressLogs.value.push(
      `任务「${task?.title || "未知"}」补充搜索：${refined}（原因：${reason}）`
    );
    return;
  }

  if (event.type === "tool_call") {
    const eventId =
      typeof payload.event_id === "number" ? payload.event_id : Date.now();
    const agent = extractOptionalString(payload.agent) ?? "Agent";
    const tool = extractOptionalString(payload.tool) ?? "tool";
    const parameters = ensureRecord(payload.parameters);
    const result = typeof payload.result === "string" ? payload.result : "";
    const noteId = extractOptionalString(payload.note_id);
    const notePath = extractOptionalString(payload.note_path);
    const task = findTask(payload.task_id);

    if (task) {
      task.toolCalls.push({
        eventId,
        agent,
        tool,
        parameters,
        result,
        noteId,
        notePath,
        timestamp: Date.now(),
      });
      if (noteId) task.noteId = noteId;
      if (notePath) task.notePath = notePath;
      progressLogs.value.push(`${agent} 调用了 ${tool}（任务 ${task.id}）`);
      if (activeTaskId.value === task.id) {
        pulse(toolHighlight);
      }
    } else {
      progressLogs.value.push(`${agent} 调用了 ${tool}`);
    }
    return;
  }

  if (event.type === "final_report") {
    const report = extractOptionalString(payload.report) ?? "";
    reportMarkdown.value = report || "报告生成失败，未获得有效内容";
    pulse(reportHighlight);
    progressLogs.value.push("最终报告已生成");
    return;
  }

  if (event.type === "error") {
    if (
      typeof payload.last_resumable_parent === "string" &&
      payload.last_resumable_parent.trim()
    ) {
      resumableRunId.value = payload.last_resumable_parent.trim();
    }
    error.value = extractOptionalString(payload.detail) ?? "研究过程中发生错误";
    progressLogs.value.push("研究失败，已停止流程");
  }
}

const handleSubmit = async () => {
  if (!form.topic.trim()) {
    error.value = "请输入研究主题";
    return;
  }

  const topic = form.topic.trim();

  if (currentController) {
    currentController.abort();
    currentController = null;
  }

  resetWorkflowState();
  currentTopic.value = topic;
  loading.value = true;

  const controller = new AbortController();
  currentController = controller;

  try {
    await runResearchStream(
      {
        topic,
        search_api: form.searchApi || undefined,
        use_history_memory: form.useHistoryMemory,
      },
      handleStreamEvent,
      { signal: controller.signal }
    );

    if (!reportMarkdown.value) {
      reportMarkdown.value = "暂无生成的报告";
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      progressLogs.value.push("已取消当前研究任务");
    } else {
      error.value = err instanceof Error ? err.message : "请求失败";
    }
  } finally {
    loading.value = false;
    if (currentController === controller) {
      currentController = null;
    }
  }
};

const handleContinue = async () => {
  const parentRunId = resumableRunId.value || currentRunId.value;
  if (!parentRunId) {
    error.value = "未找到上一轮研究记录，无法继续";
    return;
  }
  if (!form.followupTopic.trim()) {
    error.value = "请输入追问的研究主题";
    return;
  }

  const topic = form.followupTopic.trim();

  resetWorkflowState({
    preserveRunId: true,
    preserveResumableRunId: true,
  });
  currentTopic.value = topic;
  loading.value = true;

  const controller = new AbortController();
  currentController = controller;

  const continuePayload: ContinueRequest = {
    topic,
    parent_run_id: parentRunId,
    search_api: form.searchApi || undefined,
    use_history_memory: form.useHistoryMemory,
  };

  try {
    await runContinueStream(continuePayload, handleStreamEvent, {
      signal: controller.signal,
    });

    if (!reportMarkdown.value) {
      reportMarkdown.value = "暂无生成的报告";
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      progressLogs.value.push("已取消当前追问任务");
    } else {
      error.value = err instanceof Error ? err.message : "追问失败";
    }
  } finally {
    loading.value = false;
    if (currentController === controller) {
      currentController = null;
    }
  }
};

const cancelResearch = () => {
  if (!loading.value || !currentController) {
    return;
  }
  progressLogs.value.push("正在尝试取消当前研究任务...");
  currentController.abort();
};

const startNewResearch = () => {
  if (loading.value) {
    cancelResearch();
  }
  resetWorkflowState();
  form.topic = "";
  form.followupTopic = "";
  form.searchApi = "";
  form.useHistoryMemory = true;
};

const downloadReport = () => {
  if (!reportMarkdown.value) return;
  const blob = new Blob([reportMarkdown.value], {
    type: "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const filename = `research-report-${timestamp}.md`;
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
};

onMounted(() => {
  void (async () => {
    await loadHistory();
    // Rehydrate the selected run after a browser refresh so the continuation
    // anchor is available before the user starts the next follow-up.
    let lastRunId = "";
    try {
      lastRunId = window.localStorage.getItem("helloagents:last-run-id") || "";
    } catch {
      // Local storage is optional; the history list remains authoritative.
    }
    if (lastRunId) {
      const lastRun = historyItems.value.find((item) => item.run_id === lastRunId);
      if (lastRun) {
        await openHistory(lastRun);
      }
    }
  })();
  void loadMemories();
});

onBeforeUnmount(() => {
  clearAnimations();
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
});
</script>

<style scoped>
:global(:root) {
  --color-primary: #0d9488;
  --color-primary-strong: #0f766e;
  --color-secondary: #14b8a6;
  --color-accent: #ea580c;
  --color-background: #f0fdfa;
  --color-foreground: #134e4a;
  --color-foreground-strong: #0f2f2c;
  --color-muted: #e8f1f4;
  --color-border: #b7e7de;
  --color-border-soft: #d7ece8;
  --color-surface: #ffffff;
  --color-surface-muted: #f7fbfa;
  --color-danger: #dc2626;
  --color-warning: #a16207;
  --color-success: #15803d;
}

.app-shell {
  min-height: 100vh;
  color: var(--color-foreground);
  background: var(--color-background);
  font-family: "Plus Jakarta Sans", "Noto Sans SC", "PingFang SC",
    "Microsoft YaHei", system-ui, sans-serif;
  letter-spacing: 0;
}

.app-topbar {
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--color-border-soft);
  background: #fbfffd;
}

.brand-lockup,
.topbar-actions,
.toolbar-actions,
.report-actions,
.task-row {
  display: flex;
  align-items: center;
}

.brand-lockup {
  gap: 10px;
  min-width: 0;
}

.brand-mark {
  width: 32px;
  height: 32px;
  border: 1px solid var(--color-primary);
  border-radius: 7px;
  display: grid;
  place-items: center;
  color: var(--color-primary-strong);
  flex: 0 0 auto;
}

.brand-mark svg,
.icon-button svg,
.plain-button svg,
.primary-button svg {
  width: 17px;
  height: 17px;
  stroke: currentColor;
  stroke-width: 1.8;
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.brand-title,
.brand-subtitle,
.eyebrow,
.muted-copy,
.empty-copy,
h1,
h2,
p {
  margin: 0;
}

.brand-title {
  color: var(--color-foreground-strong);
  font-size: 15px;
  font-weight: 800;
  line-height: 1.1;
}

.brand-subtitle {
  margin-top: 3px;
  color: #5f7d78;
  font-size: 12px;
}

.topbar-actions {
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.connection-pill,
.status-tag {
  min-height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: #effdf9;
  color: var(--color-primary-strong);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.connection-pill {
  min-height: 36px;
  padding: 0 12px;
}

.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-primary);
}

.research-workbench {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr) 340px;
  min-height: calc(100vh - 57px);
}

.left-rail,
.inspector {
  min-width: 0;
  background: var(--color-surface-muted);
  padding: 18px;
  overflow-y: auto;
}

.left-rail {
  border-right: 1px solid var(--color-border-soft);
}

.inspector {
  border-left: 1px solid var(--color-border-soft);
}

.center-pane {
  min-width: 0;
  padding: 18px;
  background: var(--color-surface);
  overflow-y: auto;
}

.surface-card {
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: var(--color-surface);
  padding: 14px;
}

.history-list {
  display: grid;
  gap: 8px;
}

.history-item {
  width: 100%;
  display: grid;
  gap: 4px;
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: #fbfffd;
  padding: 10px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 180ms ease, border-color 180ms ease;
}

.history-item:hover:not(:disabled) {
  background: #f1fffb;
  border-color: var(--color-primary);
}

.history-item-title {
  color: var(--color-foreground-strong);
  font-size: 13px;
  font-weight: 800;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.history-item-meta,
.history-item-excerpt {
  color: #5f7d78;
  font-size: 12px;
  line-height: 1.45;
}

.history-item-excerpt {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.history-more {
  margin-top: 10px;
}

.memory-form {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}

.memory-form .field-row {
  align-items: center;
}

.memory-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.memory-item {
  display: grid;
  gap: 8px;
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: #fbfffd;
  padding: 10px;
}

.memory-item-main,
.memory-item-actions {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.memory-item-text {
  color: var(--color-foreground-strong);
  font-size: 13px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.memory-item-actions {
  justify-content: flex-end;
}

.danger-link {
  color: #b34f5f;
}

.memory-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #53716d;
  font-size: 12px;
  line-height: 1.45;
  cursor: pointer;
}

.memory-toggle input {
  width: 15px;
  height: 15px;
  accent-color: var(--color-primary);
}

.surface-card + .surface-card,
.pane-toolbar + .surface-card,
.surface-card + .pane-toolbar {
  margin-top: 14px;
}

.research-card h1,
.empty-state h1,
.pane-toolbar h1 {
  color: var(--color-foreground-strong);
  font-size: 22px;
  line-height: 1.25;
  letter-spacing: 0;
}

.research-card h1 {
  margin-top: 4px;
}

h2 {
  color: var(--color-foreground-strong);
  font-size: 16px;
  line-height: 1.35;
  letter-spacing: 0;
}

.eyebrow {
  color: #5f7d78;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0;
}

.muted-copy,
.empty-copy {
  color: #53716d;
  font-size: 13px;
  line-height: 1.55;
}

.research-card .muted-copy,
.empty-state p,
.pane-toolbar .muted-copy {
  margin-top: 8px;
}

.research-form {
  display: grid;
  gap: 10px;
  margin-top: 16px;
}

.field {
  display: grid;
  gap: 8px;
}

.field span {
  color: var(--color-foreground);
  font-size: 12px;
  font-weight: 800;
}

textarea,
select {
  width: 100%;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: #fbfffd;
  color: var(--color-foreground-strong);
  font: inherit;
  font-size: 14px;
  line-height: 1.6;
  transition: border-color 180ms ease, box-shadow 180ms ease,
    background 180ms ease;
}

textarea {
  min-height: 132px;
  resize: vertical;
  padding: 12px;
}

select {
  min-height: 38px;
  padding: 0 10px;
}

textarea:focus,
select:focus,
button:focus-visible {
  outline: 3px solid rgba(13, 148, 136, 0.22);
  outline-offset: 1px;
  border-color: var(--color-primary);
}

.field-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: end;
}

.compact-field {
  min-width: 0;
}

.icon-button,
.plain-button,
.primary-button,
.link-button {
  min-height: 36px;
  border-radius: 8px;
  border: 1px solid var(--color-border-soft);
  background: #ffffff;
  color: var(--color-foreground);
  font: inherit;
  font-size: 13px;
  font-weight: 800;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  cursor: pointer;
  transition: background 180ms ease, border-color 180ms ease, color 180ms ease,
    opacity 180ms ease;
}

.icon-button {
  width: 36px;
  padding: 0;
}

.plain-button,
.primary-button {
  padding: 0 12px;
}

.primary-button {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #ffffff;
}

.plain-button:hover:not(:disabled),
.icon-button:hover:not(:disabled) {
  background: #effdf9;
  border-color: var(--color-border);
  color: var(--color-primary-strong);
}

.primary-button:hover:not(:disabled) {
  background: var(--color-primary-strong);
  border-color: var(--color-primary-strong);
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.full-button {
  width: 100%;
}

.spinner {
  animation: spin 900ms linear infinite;
}

.spinner circle {
  fill: none;
  stroke: currentColor;
  stroke-width: 3;
  stroke-dasharray: 42;
  stroke-dashoffset: 14;
}

.error-banner {
  margin-top: 12px;
  border: 1px solid #fecaca;
  border-radius: 8px;
  background: #fff1f2;
  color: var(--color-danger);
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.5;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.status-tag {
  padding: 0 8px;
}

.status-tag.pending {
  background: #f1f5f9;
  border-color: #e2e8f0;
  color: #64748b;
}

.status-tag.in_progress {
  background: #e7fff9;
  border-color: var(--color-border);
  color: var(--color-primary-strong);
}

.status-tag.completed {
  background: #ecfdf3;
  border-color: #bbf7d0;
  color: var(--color-success);
}

.status-tag.skipped,
.status-tag.failed {
  background: #fff1f2;
  border-color: #fecaca;
  color: var(--color-danger);
}

.flow-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.flow-step,
.metric,
.empty-grid div,
.source-card,
.tool-entry {
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: #fbfffd;
}

.flow-step {
  padding: 10px;
}

.flow-step strong,
.flow-step span,
.metric strong,
.metric span,
.empty-grid strong,
.empty-grid span {
  display: block;
}

.flow-step strong,
.empty-grid strong {
  color: var(--color-foreground-strong);
  font-size: 13px;
}

.flow-step span,
.empty-grid span {
  margin-top: 4px;
  color: #5f7d78;
  font-size: 12px;
  line-height: 1.45;
}

.progress-track {
  height: 8px;
  border-radius: 999px;
  background: var(--color-muted);
  overflow: hidden;
}

.progress-track span {
  display: block;
  height: 100%;
  background: var(--color-primary);
  transition: width 240ms ease;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-top: 10px;
}

.metric {
  padding: 10px;
}

.metric strong {
  color: var(--color-foreground-strong);
  font-size: 20px;
  line-height: 1.1;
}

.metric span {
  margin-top: 4px;
  color: #5f7d78;
  font-size: 12px;
}

.telemetry-copy {
  margin: 10px 0 0;
  color: #5f7d78;
  font-size: 11px;
  line-height: 1.45;
}

.task-list-card .empty-copy {
  margin-top: 8px;
}

.task-list {
  display: grid;
  gap: 8px;
}

.task-item {
  width: 100%;
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: #ffffff;
  padding: 10px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 180ms ease, border-color 180ms ease;
}

.task-item:hover,
.task-item.active {
  background: #f1fffb;
  border-color: var(--color-primary);
}

.task-row {
  justify-content: space-between;
  gap: 8px;
}

.task-title {
  color: var(--color-foreground-strong);
  font-size: 13px;
  font-weight: 800;
}

.task-intent {
  display: block;
  margin-top: 7px;
  color: #5f7d78;
  font-size: 12px;
  line-height: 1.5;
}

.empty-state {
  min-height: calc(100vh - 93px);
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.empty-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 18px;
}

.empty-grid div {
  padding: 12px;
}

.pane-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 14px;
}

.toolbar-actions {
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.task-overview {
  display: grid;
  gap: 14px;
}

.task-query h2 {
  margin-top: 4px;
}

.note-path {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
  color: #5f7d78;
  font-size: 12px;
}

.link-button {
  min-height: 24px;
  padding: 0 8px;
  border-color: var(--color-border);
  color: var(--color-primary-strong);
}

.path-text {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

.notice-list {
  border-top: 1px solid var(--color-border-soft);
  padding-top: 12px;
}

.notice-list ul {
  margin: 8px 0 0;
  padding-left: 18px;
  color: #53716d;
  font-size: 13px;
  line-height: 1.6;
}

.source-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.source-card {
  min-width: 0;
  padding: 11px;
}

.source-card a {
  display: block;
  color: var(--color-primary-strong);
  font-size: 13px;
  font-weight: 800;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-card a:hover {
  color: var(--color-accent);
}

.source-card p {
  margin: 6px 0 0;
  color: #53716d;
  font-size: 12px;
  line-height: 1.5;
}

.markdown-body {
  border: 1px solid var(--color-border-soft);
  border-radius: 8px;
  background: #fbfffd;
  color: var(--color-foreground);
  padding: 14px 16px;
  font-size: 14px;
  line-height: 1.75;
  overflow: auto;
  max-height: 460px;
}

.report-body {
  max-height: none;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 12px 0 8px;
  color: var(--color-foreground-strong);
  font-weight: 800;
  line-height: 1.4;
  letter-spacing: 0;
}

.markdown-body :deep(h1) {
  font-size: 19px;
}

.markdown-body :deep(h2) {
  font-size: 17px;
}

.markdown-body :deep(h3) {
  font-size: 15px;
}

.markdown-body :deep(h4),
.markdown-body :deep(p),
.markdown-body :deep(li) {
  font-size: 14px;
}

.markdown-body :deep(p) {
  margin: 7px 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 8px 0;
  padding-left: 20px;
}

.markdown-body :deep(li + li) {
  margin-top: 4px;
}

.markdown-body :deep(a) {
  color: var(--color-primary-strong);
}

.markdown-body :deep(code) {
  border-radius: 5px;
  background: #eef8f6;
  padding: 2px 5px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}

.markdown-body :deep(pre) {
  border-radius: 8px;
  background: #eef8f6;
  padding: 12px;
  overflow-x: auto;
}

.event-card,
.tool-card {
  max-height: 34vh;
  overflow: auto;
}

.timeline {
  display: grid;
  gap: 9px;
}

.event-row {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
}

.event-dot {
  width: 8px;
  height: 8px;
  margin-top: 6px;
  border-radius: 50%;
  background: var(--color-border);
}

.event-row.live .event-dot {
  background: var(--color-accent);
}

.event-row p {
  color: #53716d;
  font-size: 12px;
  line-height: 1.5;
}

.tool-list {
  display: grid;
  gap: 8px;
}

.tool-entry {
  padding: 10px;
}

.tool-entry strong {
  color: var(--color-foreground-strong);
  font-size: 13px;
}

.tool-agent {
  margin-top: 6px;
  color: #5f7d78;
  font-size: 12px;
}

.tool-entry pre {
  max-height: 160px;
  overflow: auto;
  margin: 8px 0 0;
  border-radius: 6px;
  background: #eef8f6;
  color: #245c57;
  padding: 8px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.evidence-drawer,
.artifacts-panel {
  display: grid;
  gap: 10px;
}

.claim-list,
.artifact-list,
.comparison-list {
  display: grid;
  gap: 7px;
}

.claim-item,
.artifact-row,
.comparison-row {
  display: grid;
  gap: 3px;
  border: 1px solid var(--color-border-soft);
  border-radius: 6px;
  background: #fbfffd;
  padding: 8px;
  font-size: 12px;
}

.claim-item strong,
.artifact-row strong,
.comparison-row strong {
  color: var(--color-foreground-strong);
}

.claim-item span,
.artifact-row span,
.comparison-row span {
  color: #5f7d78;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.artifact-row {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}

.report-actions {
  gap: 8px;
}

.report-actions .plain-button,
.report-actions .primary-button {
  flex: 1 1 0;
}

.continue-form {
  display: grid;
  gap: 10px;
}

.block-highlight {
  animation: panelPulse 900ms ease;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes panelPulse {
  0% {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 3px rgba(13, 148, 136, 0.16);
  }
  100% {
    border-color: var(--color-border-soft);
    box-shadow: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 1ms !important;
    transition-duration: 1ms !important;
    scroll-behavior: auto !important;
  }
}

@media (max-width: 1180px) {
  .research-workbench {
    grid-template-columns: 320px minmax(0, 1fr);
  }

  .inspector {
    grid-column: 1 / -1;
    border-left: 0;
    border-top: 1px solid var(--color-border-soft);
    max-height: none;
  }

  .event-card,
  .tool-card {
    max-height: none;
  }
}

@media (max-width: 780px) {
  .app-topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .topbar-actions,
  .connection-pill {
    width: 100%;
  }

  .research-workbench {
    grid-template-columns: 1fr;
  }

  .left-rail,
  .inspector {
    border: 0;
    border-bottom: 1px solid var(--color-border-soft);
    overflow: visible;
  }

  .center-pane {
    overflow: visible;
  }

  .field-row,
  .flow-strip,
  .metric-grid,
  .source-list,
  .empty-grid,
  .report-actions {
    grid-template-columns: 1fr;
  }

  .pane-toolbar,
  .section-heading {
    flex-direction: column;
    align-items: flex-start;
  }

  .toolbar-actions,
  .report-actions,
  .plain-button,
  .primary-button {
    width: 100%;
  }

  .empty-state {
    min-height: auto;
  }
}
</style>
