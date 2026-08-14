import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const fixtureUrl = new URL(
  "../../shared/fixtures/research_quality_v1.json",
  import.meta.url,
);
const reportSourceUrl = new URL("../src/components/ResearchReport.vue", import.meta.url);
const drawerSourceUrl = new URL("../src/components/EvidenceDrawer.vue", import.meta.url);
const appSourceUrl = new URL("../src/App.vue", import.meta.url);

const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
const reportSource = await readFile(reportSourceUrl, "utf8");
const drawerSource = await readFile(drawerSourceUrl, "utf8");
const appSource = await readFile(appSourceUrl, "utf8");

const moduleScript = appSource.match(/<script lang="ts">([\s\S]*?)<\/script>/)?.[1];
assert.ok(moduleScript, "App.vue must expose its hydration guard for deterministic tests");
const compiledGuard = ts.transpileModule(moduleScript, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const { createResearchHydrationGuard } = await import(
  `data:text/javascript;base64,${Buffer.from(compiledGuard).toString("base64")}`
);

test("shared structured summary fixture matches the paragraph UI contract", () => {
  const document = fixture.structured_summary;
  assert.equal(document.schema_version, 1);
  assert.ok(Array.isArray(document.paragraphs));
  assert.ok(document.paragraphs.length > 0);
  for (const paragraph of document.paragraphs) {
    assert.match(paragraph.paragraph_id, /^para_[a-f0-9]{20}$/);
    assert.ok(["factual", "analysis", "limitation"].includes(paragraph.paragraph_type));
    assert.ok(Array.isArray(paragraph.claim_ids));
    assert.ok(Array.isArray(paragraph.citation_ids));
  }
  assert.match(reportSource, /selectEvidence/);
  assert.match(reportSource, /citation_ids/);
});

test("shared quality fixture exposes separate explainable scores", () => {
  const assessment = fixture.quality_assessment;
  assert.equal(assessment.schema_version, 1);
  assert.equal(typeof assessment.passed, "boolean");
  for (const paragraph of assessment.paragraph_assessments) {
    for (const key of [
      "semantic_score",
      "factual_score",
      "citation_score",
      "support_confidence",
    ]) {
      assert.ok(paragraph[key] >= 0 && paragraph[key] <= 1);
    }
  }
  assert.match(drawerSource, /语义/);
  assert.match(drawerSource, /事实/);
  assert.match(drawerSource, /引用/);
});

test("evidence drawer uses semantic controls and accessible status text", () => {
  assert.match(drawerSource, /<button/);
  assert.match(drawerSource, /<fieldset/);
  assert.match(drawerSource, /<legend/);
  assert.match(drawerSource, /aria-live/);
  assert.match(drawerSource, /:aria-pressed/);
});

test("stale hydration cannot overwrite the active run", async () => {
  const guard = createResearchHydrationGuard();
  const applied = [];
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => {
      resolve = done;
    });
    return { promise, resolve };
  };
  const first = deferred();
  const second = deferred();

  guard.invalidate();
  const firstToken = guard.capture("run-a");
  const firstLoad = first.promise.then((value) => {
    if (guard.isCurrent(firstToken, "run-b")) applied.push(value);
  });

  guard.invalidate();
  const secondToken = guard.capture("run-b");
  const secondLoad = second.promise.then((value) => {
    if (guard.isCurrent(secondToken, "run-b")) applied.push(value);
  });

  second.resolve("run-b evidence");
  await secondLoad;
  first.resolve("run-a evidence");
  await firstLoad;

  assert.deepEqual(applied, ["run-b evidence"]);
  assert.match(appSource, /isCurrent\(token, currentRunId\.value\)/);
  assert.match(appSource, /isGenerationCurrent\(streamGeneration\)/);
});

test("missing explicit citation never falls back to the first evidence item", () => {
  assert.match(
    drawerSource,
    /if \(props\.selectedEvidenceId\) return explicitlySelectedEvidence\.value/,
  );
  assert.match(drawerSource, /selectedEvidenceUnavailable/);
  assert.match(drawerSource, /所选引用证据不可用，未使用其他证据代替/);
  assert.doesNotMatch(
    drawerSource,
    /find\(\(item\) => item\.evidence_id === props\.selectedEvidenceId\) \?\?\s*filteredEvidence\.value\[0\]/,
  );
});
