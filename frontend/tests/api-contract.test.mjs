import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("../src/services/api.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const testableSource = source.replace(
  /const baseURL =[\s\S]*?;\r?\n/,
  'const baseURL = "http://test";\n',
);
const compiled = ts.transpileModule(testableSource, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const api = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

function responseFrom(chunks) {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }),
  );
}

test("consumeSSE requires an explicit terminal event before EOF", async () => {
  const events = [];
  await assert.rejects(
    api.consumeSSE(
      responseFrom([
        'data: {"type":"status","run_id":"run-1"}\n\n',
      ]),
      (event) => events.push(event),
    ),
    { message: "Research stream ended before a terminal event." },
  );
  assert.deepEqual(events.map((event) => event.type), ["status"]);
});

test("consumeSSE accepts a terminal event in the final unterminated frame", async () => {
  const events = [];
  await api.consumeSSE(
    responseFrom(['data: {"type":"done","run_id":"run-1"}']),
    (event) => events.push(event),
  );
  assert.deepEqual(events.map((event) => event.type), ["done"]);
});

test("consumeSSE never logs invalid raw payload or parser details", async () => {
  const sentinel = "Authorization: Bearer frontend-secret";
  const logged = [];
  const originalError = console.error;
  console.error = (...args) => logged.push(args);
  try {
    await assert.rejects(
      api.consumeSSE(
        responseFrom([`data: {${sentinel}}\n\n`]),
        () => undefined,
      ),
      { message: "Research stream ended before a terminal event." },
    );
  } finally {
    console.error = originalError;
  }
  assert.equal(logged.length, 1);
  assert.equal(logged[0].length, 1);
  assert.equal(logged[0][0], "Failed to parse a research stream event.");
  assert.doesNotMatch(JSON.stringify(logged), /frontend-secret/);
});

test("v1.1 evidence events remain additive to the stream contract", async () => {
  const events = [];
  await api.consumeSSE(
    responseFrom([
      'data: {"type":"github_evidence","run_id":"run-1","evidence_count":3}\n\n',
      'data: {"type":"coverage_update","run_id":"run-1","coverage_score":0.8}\n\n',
      'data: {"type":"artifact_ready","run_id":"run-1","path":"artifacts/report.html"}\n\n',
      'data: {"type":"done","run_id":"run-1"}\n\n',
    ]),
    (event) => events.push(event),
  );
  assert.deepEqual(
    events.map((event) => event.type),
    ["github_evidence", "coverage_update", "artifact_ready", "done"],
  );
  assert.match(source, /github_intelligence/);
  assert.match(source, /artifact_manifest/);
  assert.match(source, /line_start/);
});

test("fetchArtifact uses the durable run artifact endpoint", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(
      url,
      "http://test/runs/run-1/artifacts/artifact_report_markdown",
    );
    assert.deepEqual(options, { headers: { Accept: "*/*" } });
    return new Response("# persisted", {
      status: 200,
      headers: {
        "Content-Type": "text/markdown",
        "Content-Disposition": 'attachment; filename="report.md"',
      },
    });
  };
  try {
    const downloaded = await api.fetchArtifact(
      "run-1",
      "artifact_report_markdown",
    );
    assert.equal(downloaded.filename, "report.md");
    assert.equal(await downloaded.blob.text(), "# persisted");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
