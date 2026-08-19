import { expect, test } from "@playwright/test";

const SOURCE = "Master Agreement\nPayment term is 60 days.\nEnd.\n";
const RULES = JSON.stringify(
  [
    {
      rule_key: "payment-term",
      title: "Payment term maximum",
      description: "Flag payment terms over the preferred limit",
      severity: "HIGH",
      definition: { max_days: 45 }
    }
  ],
  null,
  2
);

test("browser drives upload, visible stages, evidence review, COMMIT, and report", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("API connected")).toBeVisible();

  await page.getByLabel("Name").fill("Browser E2E pile");
  await page.getByLabel("Description").fill("Phase 6B-6 live browser flow");
  await page.getByRole("button", { name: "Create pile" }).click();
  await expect(page.getByText("Browser E2E pile")).toBeVisible();

  await page.getByRole("button", { name: /Rules/ }).click();
  await page.getByLabel("Rules JSON").fill(RULES);
  await page.getByRole("button", { name: "Create rule set" }).click();
  await expect(page.getByText("Payment term maximum")).toBeVisible();

  await page.getByRole("button", { name: /Documents/ }).click();
  await page.getByLabel("Logical key").fill("master-contract");
  await page.getByLabel("Source file").setInputFiles({
    name: "master-contract.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(SOURCE)
  });
  await page.getByRole("button", { name: "Ingest document" }).click();
  await expect(page.getByRole("heading", { name: "Workflow runs" })).toBeVisible();

  const scope = page.getByLabel(/Contract playbook v1/);
  await scope.check();

  for (const expectedStage of ["INGEST", "UNDERSTAND", "EXAMINE", "REVIEW"]) {
    const response = page.waitForResponse(
      (item) => item.url().includes("/advance") && item.request().method() === "POST"
    );
    await page.getByRole("button", { name: "Advance one stage" }).click();
    const result = await response;
    expect(result.ok()).toBeTruthy();
    expect((await result.json()).stage).toBe(expectedStage);
  }

  await expect(page.getByRole("heading", { name: "Review findings" })).toBeVisible();
  await expect(page.getByText("Payment term is 60 days.")).toBeVisible();

  await page.getByRole("button", { name: "Approve", exact: true }).click();

  // Production runs may be committed by the background worker as soon as the
  // human gate becomes commit-ready. In worker-less/manual environments the
  // UI still offers an explicit advance. Support both without racing them.
  const completedButton = page.getByRole("button", { name: "Run completed" });
  await page.waitForTimeout(1250);
  if (!(await completedButton.isVisible().catch(() => false))) {
    const advanceButton = page.getByRole("button", { name: "Advance after review" });
    if (await advanceButton.isVisible().catch(() => false)) {
      await advanceButton.click();
    }
  }

  await expect(completedButton).toBeVisible();
  await expect(page.getByRole("heading", { name: "Final report" })).toBeVisible();
  await expect(page.getByText("Verified committed artifact")).toBeVisible();
  await expect(page.locator(".report-content")).toContainText("# Final Report");
  await expect(page.locator(".report-content")).toContainText("Payment term");
});
