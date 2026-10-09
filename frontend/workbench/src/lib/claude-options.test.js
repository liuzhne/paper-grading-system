import { describe, expect, it } from "vitest";
import { looksLikeClaudeUrl, withClaudeOptions } from "./claude-options.js";

describe("looksLikeClaudeUrl", () => {
  it.each([
    "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic",
    "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1",
    "https://api.anthropic.com",
    "https://gw.example/v1/messages/",
    "https://api.deepseek.com/anthropic",
  ])("recognizes %s", (url) => {
    expect(looksLikeClaudeUrl(url)).toBe(true);
  });

  it.each([
    "https://api.openai.com/v1",
    "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
    "https://anthropic.example.com/v1",
    "not a url",
    "",
  ])("leaves %s to the server", (url) => {
    expect(looksLikeClaudeUrl(url)).toBe(false);
  });
});

describe("withClaudeOptions", () => {
  it("adds only the values that are set and keeps other options", () => {
    expect(withClaudeOptions({ max_concurrency: 2 }, { effort: "low", structured_output: "" }, true)).toEqual({
      max_concurrency: 2,
      effort: "low",
    });
  });

  it("removes both keys when the connection is not Claude", () => {
    expect(withClaudeOptions({ effort: "low", structured_output: "off", max_concurrency: 1 }, { effort: "low" }, false))
      .toEqual({ max_concurrency: 1 });
  });
});
