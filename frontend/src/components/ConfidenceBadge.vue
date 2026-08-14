<template>
  <span
    class="confidence-badge"
    :class="`confidence-${normalizedLevel}`"
    :aria-label="accessibleLabel"
    :title="accessibleLabel"
  >
    <span aria-hidden="true" class="confidence-dot"></span>
    {{ label }}
    <span v-if="score !== null" class="confidence-score">{{ score }}%</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from "vue";

import type { ConfidenceLevel } from "../services/api";

const props = withDefaults(
  defineProps<{
    level?: ConfidenceLevel | null;
    confidence?: number | null;
  }>(),
  {
    level: "unverified",
    confidence: null,
  },
);

const normalizedLevel = computed<ConfidenceLevel>(() =>
  props.level === "high" ||
  props.level === "medium" ||
  props.level === "low"
    ? props.level
    : "unverified",
);

const label = computed(() => {
  const labels: Record<ConfidenceLevel, string> = {
    high: "高支持",
    medium: "中支持",
    low: "低支持",
    unverified: "未验证",
  };
  return labels[normalizedLevel.value];
});

const score = computed(() => {
  if (typeof props.confidence !== "number" || !Number.isFinite(props.confidence)) {
    return null;
  }
  return Math.round(Math.max(0, Math.min(1, props.confidence)) * 100);
});

const accessibleLabel = computed(() =>
  score.value === null
    ? `证据支持状态：${label.value}`
    : `证据支持状态：${label.value}，支持置信度 ${score.value}%`,
);
</script>

<style scoped>
.confidence-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  width: fit-content;
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  white-space: nowrap;
}

.confidence-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

.confidence-high {
  color: #176b51;
  background: #e9f7f1;
}

.confidence-medium {
  color: #805c13;
  background: #fff7dd;
}

.confidence-low {
  color: #9b3b31;
  background: #fff0ed;
}

.confidence-unverified {
  color: #56645f;
  background: #f1f4f3;
}

.confidence-score {
  font-variant-numeric: tabular-nums;
}
</style>
