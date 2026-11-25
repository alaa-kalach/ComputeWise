// C6 "done when": a department admin sees this month's spend by professor and downloads the
// billing report; he manages professors and ceilings but has no approve or issue actions.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { installMocks, resetWorld, setMockPersona } from "../mocks";

beforeEach(() => {
  installMocks();
  resetWorld();
  setMockPersona(null);
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
  window.history.pushState({}, "", "/");
});

async function openAsDana() {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: /Department admin$/ }));
  await screen.findByRole("heading", { name: "Professors" });
}

const section = (heading: string) => screen.getByRole("heading", { name: heading }).closest("section") as HTMLElement;
/** A professor's row in the Professors table (his name is in the billing table too). */
const row = (name: string) =>
  within(section("Professors")).getByRole("rowheader", { name: new RegExp(name) }).closest("tr") as HTMLElement;
const gauge = (label: string) => screen.getByText(label, { selector: ".gauge-name" }).nextSibling;
/** jsdom's Blob has no .text() */
const blobText = (b: Blob) =>
  new Promise<string>((ok) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.readAsText(b); });

describe("C6: department dashboard", () => {
  it("shows this month's spend, billable and observed, per professor", async () => {
    await openAsDana();
    expect(gauge("Spent this month")).toHaveTextContent("$99.00");
    expect(gauge("Billable to professors")).toHaveTextContent("$83.80");
    expect(within(row("Karim Nassar")).getByText("$400.00 a month")).toBeInTheDocument();
    expect(within(row("Rania Khoury")).getByText("Tracking only")).toBeInTheDocument();
    expect(within(row("Rania Khoury")).getByText("$22.00 billable · $15.20 observed")).toBeInTheDocument();
    const billing = section("Billing report");
    expect(within(billing).getByRole("rowheader", { name: "Total" }).closest("tr")).toHaveTextContent("$99.00");
  });

  it("draws spend over time, with a table view", async () => {
    await openAsDana();
    const trend = section("Spend over time");
    expect(within(trend).getByRole("img", { name: /Spend per month/ })).toBeInTheDocument();
    await userEvent.click(within(trend).getByRole("button", { name: "Show as table" }));
    expect(within(trend).getAllByRole("row")).toHaveLength(3); // header + two months
  });

  it("gives a tracking-only professor a ceiling", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Set budget for Prof. Rania Khoury" }));
    await userEvent.click(screen.getByRole("button", { name: "Monthly ceiling" }));
    await userEvent.type(screen.getByLabelText("Monthly ceiling (USD)"), "300");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await within(row("Rania Khoury")).findByText("$300.00 a month")).toBeInTheDocument();
  });

  it("explains why a ceiling below what he already gave out is refused", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Set budget for Prof. Karim Nassar" }));
    const amount = screen.getByLabelText("Monthly ceiling (USD)");
    await userEvent.clear(amount);
    await userEvent.type(amount, "80");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/already given his groups \$100/)).toBeInTheDocument();
  });

  it("adds a professor, who is invited and starts as tracking only", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Add professor" }));
    await userEvent.type(screen.getByLabelText("Name"), "Prof. Hana Saab");
    await userEvent.type(screen.getByLabelText("University email"), "hana.saab@cs.example.edu");
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Add professor" }));
    expect(await screen.findByText("Prof. Hana Saab was added and invited to sign in.")).toBeInTheDocument();
    expect(within(row("Hana Saab")).getByText("Tracking only")).toBeInTheDocument();
  });

  it("won't remove a professor whose groups are still active", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Remove Prof. Karim Nassar" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove professor" }));
    expect(await screen.findByText(/still has groups with active members/)).toBeInTheDocument();
  });

  it("runs Sync now and says what it did", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText(/Synced: read \d+ keys and pods/)).toBeInTheDocument();
  });

  it("downloads the billing report as CSV", async () => {
    const created: Blob[] = [];
    URL.createObjectURL = vi.fn((b: Blob) => (created.push(b), "blob:mock"));
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    expect(click).toHaveBeenCalled();
    expect(await blobText(created[0])).toMatch(/^professor_id,professor_name,period,billable/);
    click.mockRestore();
  });

  it("has no approve, issue or pod controls: those are the professor's", async () => {
    await openAsDana();
    expect(screen.queryByRole("heading", { name: "Access requests" })).toBeNull();
    // Revoking the department's OWN account is the department's job; a student's key is not.
    expect(screen.queryByRole("button", { name: /^Approve$|Issue|Stop pod|Revoke key|Disable key|Get my key/ })).toBeNull();
  });
});

describe("C6: department accounts and the wireframe extras", () => {
  const accounts = () => section("Department accounts");
  const panel = (label: string) => within(accounts()).getByRole("heading", { name: new RegExp(label) }).closest(".panel") as HTMLElement;

  it("shows the department's accounts and who may use them, with no key anywhere", async () => {
    await openAsDana();
    expect(within(panel("CS Department RunPod")).getByText("Prof. Rania Khoury")).toBeInTheDocument();
    expect(within(panel("CS Department RunPod")).getByText("Ceiling $200.00")).toBeInTheDocument();
    expect(within(accounts()).queryByText(/Khoury lab OpenRouter/)).toBeNull(); // hers, not the department's
  });

  it("adds an account, then grants it to a professor with a ceiling", async () => {
    await openAsDana();
    await userEvent.click(within(accounts()).getByRole("button", { name: "Add account" }));
    await userEvent.click(screen.getByRole("button", { name: "RunPod" }));
    await userEvent.type(screen.getByLabelText("Name"), "Spare RunPod");
    await userEvent.type(screen.getByLabelText("RunPod API key"), "rpa_demo_key");
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Add account" }));
    expect(await screen.findByText(/Spare RunPod was added/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Grant Spare RunPod to a professor" }));
    await userEvent.selectOptions(screen.getByLabelText("Professor"), "Prof. Rania Khoury");
    await userEvent.type(screen.getByLabelText("Ceiling (USD)"), "120");
    await userEvent.click(screen.getByRole("button", { name: "Grant" }));
    expect(await within(panel("Spare RunPod")).findByText("Ceiling $120.00")).toBeInTheDocument();
  });

  it("refuses a key the provider rejects", async () => {
    await openAsDana();
    await userEvent.click(within(accounts()).getByRole("button", { name: "Add account" }));
    await userEvent.type(screen.getByLabelText("Name"), "Wrong");
    await userEvent.type(screen.getByLabelText("OpenRouter management key"), "not-a-key");
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Add account" }));
    expect(await screen.findByText(/openrouter rejected this key/)).toBeInTheDocument();
  });

  it("changes a ceiling and ends a grant", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Change Prof. Karim Nassar's ceiling on CS Department OpenRouter" }));
    const ceiling = screen.getByLabelText("Ceiling (USD)");
    await userEvent.clear(ceiling);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await within(panel("CS Department OpenRouter")).findByText("No ceiling")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "End Prof. Rania Khoury's grant on CS Department RunPod" }));
    await userEvent.click(screen.getByRole("button", { name: "End grant" }));
    expect(await screen.findByText("Prof. Rania Khoury can no longer use CS Department RunPod.")).toBeInTheDocument();
    expect(within(panel("CS Department RunPod")).queryByText("Prof. Rania Khoury")).toBeNull();
  });

  it("revokes an account after a warning about the cut-off", async () => {
    await openAsDana();
    await userEvent.click(screen.getByRole("button", { name: "Revoke CS Department OpenRouter" }));
    expect(screen.getByText(/Every OpenRouter key issued under it is disabled/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke account" }));
    expect(await screen.findByText(/Revoked: CS Department OpenRouter/)).toBeInTheDocument();
  });

  it("shows spend by platform, last sync, group names and ceiling use", async () => {
    await openAsDana();
    const platform = screen.getByText("By platform", { selector: ".gauge-name" }).closest(".gauge") as HTMLElement;
    expect(platform).toHaveTextContent("RunPod$75.30");
    expect(platform).toHaveTextContent("OpenRouter$23.70");
    expect(screen.getByText(/usage last synced 3 min ago/)).toBeInTheDocument();
    expect(within(row("Karim Nassar")).getByText("FYP Team 1, FYP Team 2")).toBeInTheDocument();
    expect(within(row("Karim Nassar")).getByText("15% used")).toBeInTheDocument(); // $61.80 of $400
  });
});
