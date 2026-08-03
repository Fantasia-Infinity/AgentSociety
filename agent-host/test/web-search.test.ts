import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createWebSearchProvider,
  DeepSeekResponsesWebSearchProvider,
} from "../src/web-search.js";

test("DeepSeek adapter forces server-side search and preserves citations", async () => {
  let requestedUrl = "";
  let requestedAuthorization = "";
  let requestedBody: Record<string, unknown> = {};
  const fetchImpl: typeof fetch = async (input, init) => {
    requestedUrl = String(input);
    requestedAuthorization = String(
      new Headers(init?.headers).get("Authorization"),
    );
    requestedBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
    return new Response(
      JSON.stringify({
        id: "resp-1",
        output: [
          {
            type: "web_search_call",
            id: "search-1",
            status: "completed",
          },
          {
            type: "message",
            content: [
              {
                type: "output_text",
                text: "Grounded answer.",
                annotations: [
                  {
                    type: "url_citation",
                    url: "https://docs.example/source",
                    title: "Primary source",
                  },
                  {
                    type: "url_citation",
                    url: "https://docs.example/source",
                    title: "Duplicate source",
                  },
                ],
              },
            ],
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };
  const provider = new DeepSeekResponsesWebSearchProvider(
    "https://api.deepseek.com/v1",
    "secret-test-key",
    "deepseek-v4-flash",
    fetchImpl,
  );

  const result = await provider.search({ query: " current information " });

  assert.equal(requestedUrl, "https://api.deepseek.com/responses");
  assert.equal(requestedAuthorization, "Bearer secret-test-key");
  assert.deepEqual(requestedBody, {
    model: "deepseek-v4-flash",
    tools: [{ type: "web_search" }],
    tool_choice: { type: "web_search" },
    input: "current information",
  });
  assert.equal(result.answer, "Grounded answer.");
  assert.equal(result.citationsProvided, true);
  assert.deepEqual(result.searchCallIds, ["search-1"]);
  assert.deepEqual(result.sources, [
    { url: "https://docs.example/source", title: "Primary source" },
  ]);
});

test("DeepSeek adapter exposes missing structured citations honestly", async () => {
  const fetchImpl: typeof fetch = async () =>
    new Response(
      JSON.stringify({
        output: [
          {
            type: "web_search_call",
            id: "search-1",
            status: "completed",
            action: {
              type: "open_page",
              url: "https://example.test/searched-but-not-cited",
            },
          },
          {
            type: "message",
            content: [
              {
                type: "output_text",
                text: "An answer without structured annotations.",
                annotations: [],
              },
            ],
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  const provider = new DeepSeekResponsesWebSearchProvider(
    "https://api.deepseek.com",
    "key",
    "deepseek-v4-flash",
    fetchImpl,
  );

  const result = await provider.search({ query: "search" });

  assert.equal(result.citationsProvided, false);
  assert.deepEqual(result.sources, []);
  assert.deepEqual(result.searchCallIds, ["search-1"]);
});

test("DeepSeek adapter refuses to report an answer without a search call", async () => {
  const fetchImpl: typeof fetch = async () =>
    new Response(
      JSON.stringify({
        output: [
          {
            type: "message",
            content: [{ type: "output_text", text: "Ungrounded answer" }],
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  const provider = new DeepSeekResponsesWebSearchProvider(
    "https://api.deepseek.com",
    "key",
    "deepseek-v4-flash",
    fetchImpl,
  );

  await assert.rejects(
    provider.search({ query: "search" }),
    /did not contain a web_search_call/u,
  );
});

test("auto mode only enables search for the official DeepSeek endpoint", () => {
  const common = {
    webSearchModel: "deepseek-v4-flash",
    remoteApiKey: "key",
  };
  assert.ok(
    createWebSearchProvider({
      ...common,
      webSearchMode: "auto",
      remoteBaseUrl: "https://api.deepseek.com",
    }),
  );
  assert.equal(
    createWebSearchProvider({
      ...common,
      webSearchMode: "auto",
      remoteBaseUrl: "https://models.example/v1",
    }),
    undefined,
  );
  assert.equal(
    createWebSearchProvider({
      ...common,
      webSearchMode: "disabled",
      remoteBaseUrl: "https://api.deepseek.com",
    }),
    undefined,
  );
});
