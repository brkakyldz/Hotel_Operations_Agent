import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown, shortId } from "./markdown";

function html(text: string): HTMLElement {
  return render(<Markdown text={text} />).container;
}

describe("Markdown (safe subset)", () => {
  it("renders bold, italic, code, paragraphs and line breaks", () => {
    const c = html("Your checkout is **14:00**.\nSee `R-1042`.\n\nThanks, *Emma*!");
    expect(c.querySelectorAll("p")).toHaveLength(2);
    expect(c.querySelector("strong")?.textContent).toBe("14:00");
    expect(c.querySelector("code")?.textContent).toBe("R-1042");
    expect(c.querySelector("em")?.textContent).toBe("Emma");
    expect(c.querySelector("br")).not.toBeNull();
  });

  it("renders bullet and numbered lists", () => {
    const c = html("Options:\n- towels\n- pillows\n\n1. first\n2. second");
    expect(Array.from(c.querySelectorAll("ul li")).map((li) => li.textContent)).toEqual([
      "towels",
      "pillows",
    ]);
    expect(c.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("keeps a numbered list's own numbers and its indented continuation lines", () => {
    const c = html("1. Towels\n   two extra\n2. Cleaning\n\nA note.\n\n3. Parking");
    const lists = c.querySelectorAll("ol");
    expect(lists).toHaveLength(2);
    expect(lists[0].getAttribute("start")).toBeNull();
    expect(Array.from(lists[0].querySelectorAll("li")).map((li) => li.textContent)).toEqual([
      "Towelstwo extra",
      "Cleaning",
    ]);
    expect(lists[0].querySelector("li br")).not.toBeNull();
    expect(lists[1].getAttribute("start")).toBe("3");
  });

  it("shows HTML as literal text and never creates links or scripts", () => {
    const c = html('<img src=x onerror="alert(1)"> <script>alert(2)</script> [site](https://x.test)');
    expect(c.querySelector("img")).toBeNull();
    expect(c.querySelector("script")).toBeNull();
    expect(c.querySelector("a")).toBeNull();
    expect(c.textContent).toContain("<script>alert(2)</script>");
    expect(c.textContent).toContain("[site](https://x.test)");
  });

  it("keeps identifiers with underscores or asterisks literal", () => {
    const c = html("That did not work: NOT_CHECKED_IN (see snake_case_name and 2*3*4).");
    expect(c.querySelector("em")).toBeNull();
    expect(c.querySelector("strong")).toBeNull();
    expect(c.textContent).toBe("That did not work: NOT_CHECKED_IN (see snake_case_name and 2*3*4).");
  });

  it("keeps unmatched markers and plain arithmetic intact", () => {
    const c = html("2 * 3 = 6 and a lone ** marker");
    expect(c.querySelector("strong")).toBeNull();
    expect(c.textContent).toBe("2 * 3 = 6 and a lone ** marker");
  });
});

describe("shortId", () => {
  it("shortens long generated ids and leaves others alone", () => {
    expect(shortId("hk_b5711a42165341e0b1597e605db4864d")).toBe("hk_b5711a42…");
    expect(shortId("R-1042")).toBe("R-1042");
    expect(shortId("hk_1")).toBe("hk_1");
  });
});
