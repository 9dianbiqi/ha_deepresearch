<template>
  <div v-if="document?.paragraphs.length" class="structured-report">
    <article
      v-for="paragraph in document.paragraphs"
      :id="paragraph.paragraph_id"
      :key="paragraph.paragraph_id"
      class="report-paragraph"
      :class="{ 'report-paragraph-selected': paragraph.paragraph_id === selectedParagraphId }"
      tabindex="-1"
    >
      <div class="paragraph-meta">
        <span class="paragraph-section">{{ paragraph.section_id }}</span>
        <ConfidenceBadge
          :level="assessmentFor(paragraph.paragraph_id)?.level"
          :confidence="assessmentFor(paragraph.paragraph_id)?.support_confidence"
        />
      </div>
      <p>{{ paragraph.text }}</p>
      <div v-if="paragraph.citation_ids.length" class="paragraph-citations" aria-label="段落引用">
        <button
          v-for="(evidenceId, index) in paragraph.citation_ids"
          :key="evidenceId"
          type="button"
          class="citation-button"
          :aria-label="`查看本段第 ${index + 1} 条证据`"
          @click="selectCitation(paragraph.paragraph_id, evidenceId)"
        >
          [{{ index + 1 }}]
        </button>
      </div>
      <p
        v-else-if="paragraph.paragraph_type === 'factual'"
        class="citation-warning"
        role="status"
      >
        此事实段落尚无可验证引用
      </p>
    </article>
  </div>
  <div v-else class="markdown-body report-body" v-html="fallbackHtml"></div>
</template>

<script setup lang="ts">
import ConfidenceBadge from "./ConfidenceBadge.vue";
import type {
  ParagraphQualityAssessment,
  StructuredSummaryDocument,
  SummaryQualityAssessment,
} from "../services/api";

const props = withDefaults(
  defineProps<{
    document?: StructuredSummaryDocument | null;
    assessment?: SummaryQualityAssessment | null;
    fallbackHtml?: string;
    selectedParagraphId?: string | null;
  }>(),
  {
    document: null,
    assessment: null,
    fallbackHtml: "",
    selectedParagraphId: null,
  },
);

const emit = defineEmits<{
  selectEvidence: [evidenceId: string, paragraphId: string];
}>();

function assessmentFor(paragraphId: string): ParagraphQualityAssessment | undefined {
  return props.assessment?.paragraph_assessments.find(
    (item) => item.paragraph_id === paragraphId,
  );
}

function selectCitation(paragraphId: string, evidenceId: string): void {
  emit("selectEvidence", evidenceId, paragraphId);
}
</script>

<style scoped>
.structured-report {
  display: grid;
  gap: 14px;
}

.report-paragraph {
  border-left: 3px solid #dce9e5;
  border-radius: 0 8px 8px 0;
  background: #fbfefd;
  padding: 12px 14px;
  scroll-margin-top: 24px;
}

.report-paragraph:focus-visible,
.report-paragraph-selected {
  outline: 3px solid rgba(42, 118, 104, 0.24);
  outline-offset: 2px;
  border-left-color: #2a7668;
}

.paragraph-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 7px;
}

.paragraph-section {
  color: #506d67;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.report-paragraph p {
  margin: 0;
  color: #183934;
  line-height: 1.75;
}

.paragraph-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}

.citation-button {
  min-width: 32px;
  min-height: 32px;
  border: 1px solid #8cb9ae;
  border-radius: 6px;
  background: #eff9f5;
  color: #185e50;
  font-weight: 800;
  cursor: pointer;
}

.citation-button:hover {
  background: #dcf1e9;
}

.citation-button:focus-visible {
  outline: 3px solid rgba(42, 118, 104, 0.34);
  outline-offset: 2px;
}

.citation-warning {
  margin-top: 8px !important;
  color: #9b3b31 !important;
  font-size: 12px;
  font-weight: 700;
}

@media (max-width: 640px) {
  .report-paragraph {
    padding: 11px;
  }

  .paragraph-meta {
    align-items: flex-start;
    flex-direction: column;
  }

  .citation-button {
    min-width: 44px;
    min-height: 44px;
  }
}
</style>
