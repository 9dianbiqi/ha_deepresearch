<template>
  <section class="evidence-panel" aria-labelledby="evidence-panel-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Evidence Drawer</p>
        <h2 id="evidence-panel-title">证据与支持度</h2>
      </div>
      <span class="evidence-count">{{ filteredEvidence.length }}/{{ evidence.length }} 条</span>
    </div>

    <p class="panel-summary" aria-live="polite">
      {{ modeLabel }} · 覆盖率 {{ percent(coverageScore) }} ·
      {{ assessment?.passed === true ? "质量门禁通过" : assessment ? "存在质量风险" : "尚未评分" }}
    </p>

    <ConfidenceBadge
      v-if="selectedParagraphAssessment"
      :level="selectedParagraphAssessment.level"
      :confidence="selectedParagraphAssessment.support_confidence"
    />
    <div v-if="selectedDiagnostics.length" class="diagnostic-list" role="status">
      <p class="eyebrow">本段扣分原因</p>
      <ul>
        <li v-for="reason in selectedDiagnostics" :key="reason">{{ reason }}</li>
      </ul>
    </div>

    <fieldset class="evidence-filters">
      <legend>筛选证据</legend>
      <label>
        <input v-model="lowConfidenceOnly" type="checkbox" />
        低支持
      </label>
      <label>
        <input v-model="conflictsOnly" type="checkbox" />
        冲突证据
      </label>
      <label>
        <input v-model="missingCitationsOnly" type="checkbox" />
        无引用段落
      </label>
      <label class="dimension-filter">
        <span>研究维度</span>
        <select v-model="selectedDimension">
          <option value="">全部</option>
          <option v-for="dimension in dimensions" :key="dimension" :value="dimension">
            {{ dimension }}
          </option>
        </select>
      </label>
    </fieldset>

    <div v-if="missingCitationParagraphs.length" class="quality-notice" role="status">
      {{ missingCitationParagraphs.length }} 个事实段落缺少引用
    </div>

    <div v-if="filteredEvidence.length" class="evidence-list" aria-label="证据列表">
      <button
        v-for="item in filteredEvidence"
        :key="item.evidence_id"
        type="button"
        class="evidence-row"
        :class="{ selected: item.evidence_id === selectedEvidence?.evidence_id }"
        :aria-pressed="item.evidence_id === selectedEvidence?.evidence_id"
        @click="emit('selectEvidence', item.evidence_id)"
      >
        <strong>{{ item.title || item.evidence_id }}</strong>
        <span>{{ locationLabel(item) }}</span>
        <small>{{ item.evidence_level }} · {{ item.source.provider_id }}</small>
      </button>
    </div>
    <p v-else-if="missingCitationsOnly && missingCitationParagraphs.length" class="empty-copy">
      无引用段落没有可打开的 Evidence，请返回报告查看对应段落。
    </p>
    <p v-else class="empty-copy">当前筛选条件下没有证据。</p>

    <article v-if="selectedEvidence" class="evidence-detail" aria-live="polite">
      <div class="detail-heading">
        <div>
          <p class="eyebrow">Selected Evidence</p>
          <h3>{{ selectedEvidence.title }}</h3>
        </div>
        <a
          v-if="safeUrl(selectedEvidence.locator.url)"
          :href="selectedEvidence.locator.url"
          target="_blank"
          rel="noopener noreferrer"
        >
          查看原始位置
        </a>
      </div>
      <blockquote>{{ selectedEvidence.excerpt || "该记录未包含可展示摘录。" }}</blockquote>
      <dl>
        <div>
          <dt>位置</dt>
          <dd>{{ locationLabel(selectedEvidence) }}</dd>
        </div>
        <div>
          <dt>采集时间</dt>
          <dd>{{ selectedEvidence.source.captured_at || "未知" }}</dd>
        </div>
        <div>
          <dt>内容版本</dt>
          <dd>{{ selectedEvidence.source.resolved_version || selectedEvidence.source.content_hash || "未固定" }}</dd>
        </div>
      </dl>

      <div v-if="relatedClaims.length" class="related-claims">
        <p class="eyebrow">支持的 Claim</p>
        <article v-for="claim in relatedClaims" :key="claim.claim_id">
          <strong>{{ claim.dimension }}</strong>
          <p>{{ claim.statement }}</p>
          <div v-if="assessmentForClaim(claim.claim_id)" class="score-grid">
            <span>语义 {{ percent(assessmentForClaim(claim.claim_id)?.semantic_score) }}</span>
            <span>事实 {{ percent(assessmentForClaim(claim.claim_id)?.factual_score) }}</span>
            <span>引用 {{ percent(assessmentForClaim(claim.claim_id)?.citation_score) }}</span>
          </div>
          <ul
            v-if="assessmentForClaim(claim.claim_id)?.reasons.length"
            class="claim-reasons"
          >
            <li
              v-for="reason in assessmentForClaim(claim.claim_id)?.reasons"
              :key="reason"
            >
              {{ reason }}
            </li>
          </ul>
        </article>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import ConfidenceBadge from "./ConfidenceBadge.vue";
import type {
  ClaimSupportAssessment,
  ResearchClaim,
  ResearchEvidence,
  StructuredSummaryDocument,
  SummaryQualityAssessment,
} from "../services/api";

const props = withDefaults(
  defineProps<{
    evidence?: ResearchEvidence[];
    claims?: ResearchClaim[];
    document?: StructuredSummaryDocument | null;
    assessment?: SummaryQualityAssessment | null;
    selectedEvidenceId?: string | null;
    selectedParagraphId?: string | null;
    coverageScore?: number | null;
    mode?: string | null;
  }>(),
  {
    evidence: () => [],
    claims: () => [],
    document: null,
    assessment: null,
    selectedEvidenceId: null,
    selectedParagraphId: null,
    coverageScore: null,
    mode: null,
  },
);

const emit = defineEmits<{
  selectEvidence: [evidenceId: string];
}>();

const lowConfidenceOnly = ref(false);
const conflictsOnly = ref(false);
const missingCitationsOnly = ref(false);
const selectedDimension = ref("");

const modeLabel = computed(() => {
  if (props.mode === "github") return "GitHub 研究";
  if (props.mode === "paper") return "论文研究";
  return "Web 研究";
});

const selectedParagraphAssessment = computed(() =>
  props.assessment?.paragraph_assessments.find(
    (item) => item.paragraph_id === props.selectedParagraphId,
  ),
);

const selectedDiagnostics = computed(() => [
  ...(selectedParagraphAssessment.value?.blockers ?? []),
  ...(selectedParagraphAssessment.value?.warnings ?? []),
]);

const lowConfidenceEvidenceIds = computed(() => {
  const weakParagraphIds = new Set(
    (props.assessment?.paragraph_assessments ?? [])
      .filter(
        (item) =>
          item.level === "low" ||
          item.level === "unverified" ||
          item.support_confidence < 0.6,
      )
      .map((item) => item.paragraph_id),
  );
  return new Set(
    (props.document?.paragraphs ?? [])
      .filter((paragraph) => weakParagraphIds.has(paragraph.paragraph_id))
      .flatMap((paragraph) => paragraph.citation_ids),
  );
});

const conflictingEvidenceIds = computed(
  () =>
    new Set(
      props.claims.flatMap((claim) => claim.conflicting_evidence_ids ?? []),
    ),
);

const missingCitationParagraphs = computed(() =>
  (props.document?.paragraphs ?? []).filter(
    (paragraph) =>
      paragraph.paragraph_type === "factual" && paragraph.citation_ids.length === 0,
  ),
);

const dimensions = computed(() =>
  [...new Set(props.claims.map((claim) => claim.dimension).filter(Boolean))].sort(),
);

const evidenceIdsForDimension = computed(() => {
  if (!selectedDimension.value) return null;
  return new Set(
    props.claims
      .filter((claim) => claim.dimension === selectedDimension.value)
      .flatMap((claim) => [
        ...(claim.evidence_ids ?? []),
        ...(claim.conflicting_evidence_ids ?? []),
      ]),
  );
});

const filteredEvidence = computed(() => {
  if (missingCitationsOnly.value) return [];
  return props.evidence.filter((item) => {
    if (lowConfidenceOnly.value && !lowConfidenceEvidenceIds.value.has(item.evidence_id)) {
      return false;
    }
    if (conflictsOnly.value && !conflictingEvidenceIds.value.has(item.evidence_id)) {
      return false;
    }
    const dimensionIds = evidenceIdsForDimension.value;
    return dimensionIds === null || dimensionIds.has(item.evidence_id);
  });
});

const selectedEvidence = computed(() =>
  filteredEvidence.value.find((item) => item.evidence_id === props.selectedEvidenceId) ??
  filteredEvidence.value[0] ??
  null,
);

const relatedClaims = computed(() => {
  if (!selectedEvidence.value) return [];
  const evidenceId = selectedEvidence.value.evidence_id;
  return props.claims.filter(
    (claim) =>
      claim.evidence_ids?.includes(evidenceId) ||
      claim.conflicting_evidence_ids?.includes(evidenceId),
  );
});

function assessmentForClaim(claimId: string): ClaimSupportAssessment | undefined {
  return props.assessment?.claim_assessments.find((item) => item.claim_id === claimId);
}

function percent(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
    : "--";
}

function safeUrl(value: string): boolean {
  return /^https?:\/\//i.test(value);
}

function locationLabel(item: ResearchEvidence): string {
  const locator = item.locator;
  if (locator.file_path && typeof locator.line_start === "number") {
    const end = locator.line_end ?? locator.line_start;
    return `${locator.file_path} · L${locator.line_start}-L${end}`;
  }
  if (typeof locator.page_start === "number") {
    const end = locator.page_end ?? locator.page_start;
    return `第 ${locator.page_start}-${end} 页`;
  }
  if (locator.section || locator.paragraph) {
    return [locator.section, locator.paragraph].filter(Boolean).join(" · ");
  }
  return item.source.canonical_url || locator.url;
}
</script>

<style scoped>
.evidence-panel {
  display: grid;
  gap: 12px;
}

.panel-heading,
.detail-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.panel-heading h2,
.detail-heading h3 {
  margin: 0;
  color: #173d37;
}

.eyebrow {
  margin: 0 0 3px;
  color: #5b786f;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.evidence-count {
  border-radius: 999px;
  background: #e8f5f0;
  color: #226758;
  padding: 4px 8px;
  font-size: 11px;
  font-weight: 800;
}

.panel-summary,
.empty-copy {
  margin: 0;
  color: #5a736e;
  font-size: 12px;
  line-height: 1.5;
}

.evidence-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  border: 1px solid #d7e7e2;
  border-radius: 8px;
  padding: 9px;
}

.evidence-filters legend {
  padding: 0 4px;
  color: #355f57;
  font-size: 11px;
  font-weight: 800;
}

.evidence-filters label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #355f57;
  font-size: 12px;
}

.dimension-filter select {
  min-height: 32px;
  border: 1px solid #a9c9c0;
  border-radius: 6px;
  background: white;
  padding: 3px 6px;
}

.quality-notice {
  border-left: 3px solid #a9443a;
  background: #fff0ed;
  color: #8a342c;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 700;
}

.diagnostic-list {
  border: 1px solid #e1c983;
  border-radius: 7px;
  background: #fff9e7;
  color: #6f5215;
  padding: 8px 10px;
}

.diagnostic-list ul,
.claim-reasons {
  margin: 5px 0 0;
  padding-left: 18px;
  font-size: 11px;
  line-height: 1.5;
}

.evidence-list {
  display: grid;
  gap: 6px;
  max-height: 300px;
  overflow: auto;
  padding: 2px;
}

.evidence-row {
  display: grid;
  gap: 3px;
  width: 100%;
  border: 1px solid #d8e7e2;
  border-radius: 8px;
  background: #fbfefd;
  color: #274e47;
  padding: 9px;
  text-align: left;
  cursor: pointer;
}

.evidence-row:hover,
.evidence-row.selected {
  border-color: #4f9788;
  background: #eef9f5;
}

.evidence-row:focus-visible,
.detail-heading a:focus-visible,
.dimension-filter select:focus-visible {
  outline: 3px solid rgba(42, 118, 104, 0.34);
  outline-offset: 2px;
}

.evidence-row span,
.evidence-row small {
  color: #5a736e;
  overflow-wrap: anywhere;
}

.evidence-detail {
  display: grid;
  gap: 10px;
  border-top: 1px solid #d8e7e2;
  padding-top: 12px;
}

.detail-heading a {
  color: #176b5b;
  font-size: 12px;
  font-weight: 700;
}

.evidence-detail blockquote {
  margin: 0;
  border-left: 3px solid #7daf9f;
  background: #f3faf7;
  color: #274e47;
  padding: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.evidence-detail dl {
  display: grid;
  gap: 5px;
  margin: 0;
}

.evidence-detail dl div {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  gap: 7px;
  font-size: 11px;
}

.evidence-detail dt {
  color: #658079;
  font-weight: 700;
}

.evidence-detail dd {
  margin: 0;
  color: #294e47;
  overflow-wrap: anywhere;
}

.related-claims {
  display: grid;
  gap: 7px;
}

.related-claims article {
  border: 1px solid #dbe9e5;
  border-radius: 7px;
  padding: 8px;
}

.related-claims p {
  margin: 4px 0 0;
  color: #355f57;
  font-size: 12px;
  line-height: 1.5;
}

.score-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
  margin-top: 7px;
}

.score-grid span {
  border-radius: 5px;
  background: #edf6f3;
  color: #315f56;
  padding: 5px;
  font-size: 10px;
  text-align: center;
}

.claim-reasons {
  color: #6f5215;
}

@media (max-width: 640px) {
  .panel-heading,
  .detail-heading {
    flex-direction: column;
  }

  .evidence-filters {
    align-items: stretch;
    flex-direction: column;
  }

  .evidence-filters label,
  .evidence-row {
    min-height: 44px;
  }

  .score-grid {
    grid-template-columns: 1fr;
  }
}
</style>
