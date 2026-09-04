import http from "k6/http";
import { check } from "k6";

const baseUrl = __ENV.BASE_URL || "http://kong:8000";
const rps = Number(__ENV.PROVIDER_RPS || __ENV.LOAD_RPS || 10);
const duration = __ENV.LOAD_DURATION || "240s";
const products = ["VIAJE", "DISPOSITIVO", "VIDA_MICRO"];
const partners = (__ENV.PARTNERS || "partner-a,partner-b,partner-c").split(",");

export const options = {
  scenarios: {
    provider_control: {
      executor: "constant-arrival-rate",
      rate: rps,
      timeUnit: "1s",
      duration,
      preAllocatedVUs: Math.max(5, rps),
      maxVUs: Math.max(20, rps * 4),
    },
  },
  thresholds: {
    http_req_failed: ["rate==0"],
    http_req_duration: ["p(95)<250", "p(99)<500"],
  },
};

export default function () {
  const product = products[__ITER % products.length];
  const partner = partners[__ITER % partners.length];
  const response = http.get(`${baseUrl}/v1/provider-quote?product_code=${product}`, {
    headers: { "X-Partner-Id": partner },
    tags: { route: "provider-control" },
  });

  check(response, {
    "provider status is not 5xx": (r) => r.status < 500,
    "provider has body": (r) => r.body && r.body.length > 0,
  });
}
