import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";

const baseUrl = __ENV.BASE_URL || "http://kong:8000";
const rps = Number(__ENV.LOAD_RPS || 50);
const providerRps = Number(__ENV.PROVIDER_RPS || Math.max(1, Math.floor(rps / 5)));
const duration = __ENV.LOAD_DURATION || "240s";
const partners = (__ENV.PARTNERS || "partner-a,partner-b,partner-c").split(",");

const quotes = new SharedArray("quotes", () => JSON.parse(open("./data/quotes.json")));

export const options = {
  scenarios: {
    quotes: {
      executor: "constant-arrival-rate",
      rate: rps,
      timeUnit: "1s",
      duration,
      preAllocatedVUs: Math.max(20, rps),
      maxVUs: Math.max(100, rps * 4),
      exec: "quoteJourney",
    },
    provider_control: {
      executor: "constant-arrival-rate",
      rate: providerRps,
      timeUnit: "1s",
      duration,
      preAllocatedVUs: Math.max(5, providerRps),
      maxVUs: Math.max(20, providerRps * 4),
      exec: "providerControl",
    },
  },
  thresholds: {
    http_req_failed: ["rate==0"],
    "http_req_duration{route:quotes}": ["p(95)<250", "p(99)<500"],
    "http_req_duration{route:provider-control}": ["p(95)<250", "p(99)<500"],
  },
};

export function quoteJourney() {
  const i = __ITER % quotes.length;
  const payload = quotes[i];
  const partner = partners[i % partners.length];
  const response = http.post(`${baseUrl}/v1/quotes`, JSON.stringify(payload), {
    headers: {
      "Content-Type": "application/json",
      "X-Partner-Id": partner,
    },
    tags: { route: "quotes" },
  });

  check(response, {
    "quote status is not 5xx": (r) => r.status < 500,
    "quote has body": (r) => r.body && r.body.length > 0,
  });
}

export function providerControl() {
  const product = quotes[__ITER % quotes.length].product_code;
  const partner = partners[__ITER % partners.length];
  const response = http.get(`${baseUrl}/v1/provider-quote?product_code=${product}`, {
    headers: { "X-Partner-Id": partner },
    tags: { route: "provider-control" },
  });

  check(response, {
    "provider status is not 5xx": (r) => r.status < 500,
    "provider has body": (r) => r.body && r.body.length > 0,
  });

  sleep(0);
}

export default function () {
  quoteJourney();
}
