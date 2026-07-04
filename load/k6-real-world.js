import http from "k6/http";
import { check, sleep } from "k6";
import exec from "k6/execution";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8765";
const AUTHORIZATION = __ENV.AUTHORIZATION || "Bearer sk-ant-oat01-SIM-A";
const MODEL = __ENV.MODEL || "claude-opus-4-1";
const BIG_PROMPT_BYTES = Number(__ENV.BIG_PROMPT_BYTES || 65536);
const THINK_S = Number(__ENV.THINK_S || 0.2);

export const options = {
  scenarios: {
    coding_agents: {
      executor: "ramping-vus",
      stages: [
        { duration: "20s", target: 8 },
        { duration: "60s", target: 8 },
        { duration: "20s", target: 0 },
      ],
      exec: "codingAgent",
    },
    evaluator_hooks: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.EVAL_RATE || 2),
      timeUnit: "1s",
      duration: "90s",
      preAllocatedVUs: 4,
      maxVUs: 16,
      exec: "evaluatorHook",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<15000"],
  },
};

function headers() {
  return {
    authorization: AUTHORIZATION,
    "content-type": "application/json",
    "x-throttle-client-id": `${exec.scenario.name}-${__VU}`,
  };
}

function message(maxTokens, bodyBytes, withTools, stream) {
  const content = bodyBytes > 0 ? "x".repeat(bodyBytes) : "ping";
  const payload = {
    model: MODEL,
    max_tokens: maxTokens,
    stream,
    messages: [{ role: "user", content }],
  };
  if (withTools) {
    payload.tools = [
      {
        name: "shell",
        description: "simulation tool",
        input_schema: { type: "object", properties: {} },
      },
    ];
  }
  const res = http.post(`${BASE_URL}/v1/messages`, JSON.stringify(payload), {
    headers: headers(),
    timeout: "120s",
  });
  check(res, {
    "status is useful": (r) => [200, 401, 429, 503, 529].includes(r.status),
    "no proxy 5xx": (r) => r.status < 500 || r.status === 503 || r.status === 529,
  });
  return res;
}

export function codingAgent() {
  message(8192, BIG_PROMPT_BYTES, true, true);
  sleep(THINK_S);
}

export function evaluatorHook() {
  message(1024, 1024, false, false);
  sleep(THINK_S);
}
