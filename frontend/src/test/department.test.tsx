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
    expect(screen.queryByRole("button", { name: /^Approve$|Issue|Stop pod|Revoke/ })).toBeNull();
  });
});
