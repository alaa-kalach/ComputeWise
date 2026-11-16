// The C1 "done when" (sign in, land on a dashboard per role, switch views) and the C4 flows,
// on the mocks, through the real components.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { installMocks, resetWorld, setMockPersona } from "../mocks";

beforeEach(() => {
  installMocks();
  resetWorld();
  setMockPersona(null);
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
  window.history.pushState({}, "", "/");
});

async function signInAs(name: RegExp) {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name }));
}

describe("C1: shell", () => {
  it("lands each role on its own dashboard", async () => {
    await signInAs(/Prof\. Karim Nassar/);
    expect(await screen.findByRole("heading", { name: "Access requests" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/professor");
  });

  it("offers a view switch to someone with two roles", async () => {
    await signInAs(/Admin who is also a professor/);
    expect(await screen.findByRole("heading", { name: "Department dashboard" })).toBeInTheDocument();
    const views = screen.getByRole("group", { name: "Switch view" });
    await userEvent.click(within(views).getByRole("button", { name: "Professor" }));
    expect(await screen.findByRole("heading", { name: "Access requests" })).toBeInTheDocument();
  });

  it("shows no view switch for a single role, and signs out", async () => {
    await signInAs(/Omar Itani/);
    await screen.findByText(/You can spend/);
    expect(screen.queryByRole("group", { name: "Switch view" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(/pick someone from the seed data/)).toBeInTheDocument();
  });
});

describe("C4: user dashboard", () => {
  it("shows each professor's budget separately", async () => {
    await signInAs(/Sara Mansour/);
    const hero = await screen.findByRole("heading", { level: 1 });
    expect(hero).toHaveTextContent("You can spend $11.50 in FYP Team 1 and $24.80 in Research Lab this month.");
    expect(screen.getByRole("heading", { name: "FYP Team 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Research Lab" })).toBeInTheDocument();
    expect(screen.getAllByRole("meter")).toHaveLength(2);
  });

  it("requests access and shows it as waiting", async () => {
    await signInAs(/Lina Saleh/);
    const ai = (await screen.findByRole("heading", { name: "AI models" })).closest(".panel") as HTMLElement;
    await userEvent.click(within(ai).getByRole("button", { name: "Request access" }));
    await userEvent.type(screen.getByLabelText("What do you need it for?"), "Captions for the demo");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await within(ai).findByText("Waiting for your professor")).toBeInTheDocument();
  });

  it("warns when the team budget runs low", async () => {
    await signInAs(/Lina Saleh/);
    expect(await screen.findByText(/has used 82% of its \$50/)).toBeInTheDocument();
    expect(screen.getByText("Running low")).toBeInTheDocument();
  });

  it("launches a pod from the form", async () => {
    await signInAs(/Omar Itani/);
    await userEvent.click(await screen.findByRole("button", { name: "Launch a pod" }));
    await userEvent.click(await screen.findByRole("radio", { name: /RTX A5000/ }));
    expect(screen.getByRole("radio", { name: /H100 PCIe/ })).toBeDisabled(); // none free
    await userEvent.click(screen.getByRole("button", { name: "Launch RTX A5000" }));
    expect(await screen.findByText(/RunPod is starting the machine/)).toBeInTheDocument();
    expect(screen.getAllByText("Starting").length).toBeGreaterThan(0);
  });
});
