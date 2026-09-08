/**
 * Step definitions for tests/bdd/features/patterns-graph.feature.
 *
 * Sign-in is driven through window.__e2e__ (tests/bdd/fakes/e2eAuthState.ts),
 * which only exists in this bundle because vite.config.ts aliases
 * ./lib/firebase and firebase/auth to fakes when VITE_E2E=true
 * (playwright.config.ts's webServer.env) — see those files for the full
 * picture. The two graph endpoints are mocked per scenario via
 * page.route(); no real ADK backend, Firebase project, or credentials run
 * anywhere in this suite.
 */
import { expect } from "@playwright/test";
import { createBdd } from "playwright-bdd";
import { FakeUser } from "../../fakes/e2eAuthState";

const { Given, When, Then } = createBdd();

const TEST_USER: FakeUser = { uid: "e2e-user", displayName: "E2E Journal User", email: "e2e@example.test" };

Given("I am signed in", async ({ page }) => {
  await page.goto("/");
  await page.evaluate((user) => window.__e2e__.signIn(user), TEST_USER);
});

Given("my relationship graph has two connected reflections", async ({ page }) => {
  await page.route("**/api/graph/relationships", (route) =>
    route.fulfill({
      json: {
        nodes: [
          { id: "entry-a", summary: "A calm morning walk" },
          { id: "entry-b", summary: "Another calm morning walk" },
        ],
        edges: [{ source: "entry-a", target: "entry-b", reason: "similar reflections (score 0.98)" }],
      },
    }),
  );
  await page.route("**/api/graph/emotional-patterns", (route) =>
    route.fulfill({ json: { patterns: [], graph: { nodes: [], edges: [] } } }),
  );
});

Given("my emotional pattern graph has a {string} to {string} pattern", async ({ page }, trigger: string, emotion: string) => {
  await page.route("**/api/graph/relationships", (route) => route.fulfill({ json: { nodes: [], edges: [] } }));
  await page.route("**/api/graph/emotional-patterns", (route) =>
    route.fulfill({
      json: {
        patterns: [{ trigger, emotion, mention_count: 2, average_intensity: 0.7, example_phrases: ["a work example"] }],
        graph: {
          nodes: [
            { id: `trigger:${trigger}`, kind: "trigger", label: trigger },
            { id: `emotion:${emotion}`, kind: "emotion", label: emotion },
          ],
          edges: [{ trigger, emotion, mention_count: 2, average_intensity: 0.7 }],
        },
      },
    }),
  );
});

Given("I have no analyzed reflections", async ({ page }) => {
  await page.route("**/api/graph/relationships", (route) => route.fulfill({ json: { nodes: [], edges: [] } }));
  await page.route("**/api/graph/emotional-patterns", (route) =>
    route.fulfill({ json: { patterns: [], graph: { nodes: [], edges: [] } } }),
  );
});

When("I open the patterns view", async ({ page }) => {
  await page.getByRole("button", { name: /view reflection patterns/i }).click();
});

When("I switch to the emotional patterns tab", async ({ page }) => {
  await page.getByRole("tab", { name: /emotional patterns/i }).click();
});

Then("I see a graph of related reflections", async ({ page }) => {
  await expect(page.getByRole("img", { name: /reflection graph visualization/i })).toBeVisible();
});

Then("the graph shows {int} reflections", async ({ page }, count: number) => {
  await expect(page.locator(".graph-node")).toHaveCount(count);
});

Then("I see a graph of triggers and emotions", async ({ page }) => {
  await expect(page.getByRole("img", { name: /reflection graph visualization/i })).toBeVisible();
  await expect(page.locator(".graph-node")).toHaveCount(2);
});

Then("I see the pattern {string}", async ({ page }, text: string) => {
  await expect(page.getByText(text, { exact: false })).toBeVisible();
});

Then("I see a message instead of a graph", async ({ page }) => {
  await expect(page.getByRole("status")).toContainText(/no related reflections yet/i);
});
