import request from "supertest";
import { describe, expect, it, vi } from "vitest";
import { createApp, parseInteraction, shouldAdvanceFallback } from "../server";

type Data = Record<string, any>;

/** Minimal Firestore transaction double; production quota correctness is also
 * exercised against the emulator in the documented release test plan. */
function memoryFirestore() {
  const documents = new Map<string, Data>();
  const valueAt = (data: Data, field: string) => field.split(".").reduce((value, key) => value?.[key], data);
  const snapshot = (path: string) => ({ exists: documents.has(path), data: () => documents.get(path) });
  const directDocs = (collectionPath: string) => [...documents.entries()]
    .filter(([path]) => path.startsWith(`${collectionPath}/`) && path.split("/").length === collectionPath.split("/").length + 1)
    .map(([id, data]) => ({ id: id.slice(collectionPath.length + 1), data: () => data }));
  const collection = (collectionPath: string) => {
    let equality: [string, unknown] | undefined;
    let sort: [string, "asc" | "desc"] | undefined;
    let maximum: number | undefined;
    const query: any = {
      where: (field: string, _operator: string, value: unknown) => { equality = [field, value]; return query; },
      orderBy: (field: string, direction: "asc" | "desc" = "asc") => { sort = [field, direction]; return query; },
      limit: (count: number) => { maximum = count; return query; },
      get: async () => {
        let docs = directDocs(collectionPath);
        if (equality) docs = docs.filter((doc) => valueAt(doc.data(), equality![0]) === equality![1]);
        if (sort) docs.sort((a, b) => {
          const left = valueAt(a.data(), sort![0]);
          const right = valueAt(b.data(), sort![0]);
          const leftValue = typeof left?.toMillis === "function" ? left.toMillis() : left;
          const rightValue = typeof right?.toMillis === "function" ? right.toMillis() : right;
          const comparison = leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
          return sort![1] === "desc" ? -comparison : comparison;
        });
        return { empty: docs.length === 0, docs: maximum === undefined ? docs : docs.slice(0, maximum) };
      },
    };
    return query;
  };
  const doc = (path: string) => ({ kind: "doc", path, get: async () => snapshot(path), set: async (data: Data, options?: { merge?: boolean }) => {
    documents.set(path, options?.merge ? { ...(documents.get(path) || {}), ...data } : data);
  } });
  const transaction = {
    get: async (reference: any) => reference.kind === "doc" ? snapshot(reference.path) : reference.get(),
    set: (reference: any, data: Data, options?: { merge?: boolean }) => {
      documents.set(reference.path, options?.merge ? { ...(documents.get(reference.path) || {}), ...data } : data);
    },
    update: (reference: any, data: Data) => {
      if (!documents.has(reference.path)) throw new Error("missing document");
      documents.set(reference.path, { ...documents.get(reference.path), ...data });
    },
  };
  return {
    doc,
    collection,
    runTransaction: async (callback: (tx: typeof transaction) => unknown) => callback(transaction),
    documents,
  };
}

function backend(options?: { appCheckFails?: boolean; generate?: (model: string) => Promise<string> }) {
  const db = memoryFirestore();
  let now = new Date("2026-08-26T00:00:00.000Z");
  const generate = options?.generate || vi.fn(async (model: string) => model.includes("3.6") ? "Reflection" : "Short title");
  const app = createApp({
    db: db as any,
    projectId: "genai-academy-temp",
    now: () => now,
    auth: { verifyIdToken: vi.fn(async () => ({ uid: "userA", aud: "genai-academy-temp", iss: "https://securetoken.google.com/genai-academy-temp", exp: 2_000_000_000 })) } as any,
    appCheck: { verifyToken: vi.fn(async () => { if (options?.appCheckFails) throw new Error("invalid"); }) },
    generate: async (model, _request) => generate(model),
  });
  return { app, db, generate, advance: (milliseconds: number) => { now = new Date(now.getTime() + milliseconds); } };
}

const secureHeaders = { Authorization: "Bearer valid-id-token", "X-Firebase-AppCheck": "valid-app-check-token" };
const validBody = (id = "request_id_0001") => ({ prompt: "I felt focused after a short walk.", sessionId: "session_id_0001", idempotencyRequestId: id });

describe("backend security boundary", () => {
  it("advances the fallback ladder only for the four approved numeric statuses", () => {
    for (const status of [503, 429, 404, 500]) expect(shouldAdvanceFallback({ status })).toBe(true);
    for (const status of [400, 401, 403, 408, 409, 502, 504]) expect(shouldAdvanceFallback({ status })).toBe(false);
    expect(shouldAdvanceFallback(new Error("503 in a message is not authority"))).toBe(false);
  });

  it("strictly parses only the three server-owned interaction fields", () => {
    expect(parseInteraction(validBody())).toMatchObject(validBody());
    expect(parseInteraction({ ...validBody(), history: [] })).toBeNull();
    expect(parseInteraction({ ...validBody(), uid: "userB" })).toBeNull();
    expect(parseInteraction({ ...validBody(), prompt: "x".repeat(1001) })).toBeNull();
  });

  it("rejects malformed, oversized, unauthenticated, and invalid-App-Check calls before generation", async () => {
    const unauthenticated = backend();
    await request(unauthenticated.app).post("/api/journal/interaction").send(validBody()).expect(401);
    await request(unauthenticated.app).post("/api/journal/interaction").set("Content-Type", "application/json").send("{not json").expect(400);
    await request(unauthenticated.app).post("/api/journal/interaction").set("Content-Type", "application/json").send({ ...validBody(), prompt: "x".repeat(5000) }).expect(413);
    expect(unauthenticated.generate).not.toHaveBeenCalled();

    const rejectedAppCheck = backend({ appCheckFails: true });
    await request(rejectedAppCheck.app).post("/api/journal/interaction").set(secureHeaders).send(validBody()).expect(401);
    expect(rejectedAppCheck.generate).not.toHaveBeenCalled();
  });

  it("counts completed interactions separately from charged attempts and caches completed IDs", async () => {
    const service = backend();
    const first = await request(service.app).post("/api/journal/interaction").set(secureHeaders).send(validBody()).expect(200);
    expect(first.body.status).toBe("completed");
    expect(service.generate).toHaveBeenCalledTimes(2);
    await request(service.app).post("/api/journal/interaction").set(secureHeaders).send(validBody()).expect(200);
    expect(service.generate).toHaveBeenCalledTimes(2);
    const quota = await request(service.app).get("/api/journal/quota").set("Authorization", secureHeaders.Authorization).expect(200);
    expect(quota.body).toMatchObject({ completedInteractionCount: 1, completedInteractionLimit: 10, monthlyAttemptCount: 2, monthlyAttemptLimit: 80 });
  });

  it("admits at most ten completed interaction units while leaving model attempts separately charged", async () => {
    const service = backend();
    for (let index = 0; index < 10; index += 1) {
      await request(service.app)
        .post("/api/journal/interaction")
        .set(secureHeaders)
        .send(validBody(`request_id_${String(index).padStart(4, "0")}`))
        .expect(200);
      service.advance(60_000);
    }
    await request(service.app).post("/api/journal/interaction").set(secureHeaders).send(validBody("request_id_0010")).expect(429);
    const quota = await request(service.app).get("/api/journal/quota").set("Authorization", secureHeaders.Authorization).expect(200);
    expect(quota.body).toMatchObject({ completedInteractionCount: 10, monthlyAttemptCount: 20 });
  });

  it("retries a failed same-ID request without another minute admission, while keeping attempts charged", async () => {
    let calls = 0;
    const service = backend({ generate: async () => {
      calls += 1;
      if (calls === 1) throw { status: 400 };
      return calls === 2 ? "Reflection" : "Short title";
    } });
    await request(service.app).post("/api/journal/interaction").set(secureHeaders).send(validBody()).expect(502);
    await request(service.app).post("/api/journal/interaction").set(secureHeaders).send(validBody()).expect(200);
    const quota = await request(service.app).get("/api/journal/quota").set("Authorization", secureHeaders.Authorization).expect(200);
    expect(quota.body).toMatchObject({ completedInteractionCount: 1, monthlyAttemptCount: 3 });
  });
});
