import { Fragment, type ReactNode } from "react";

/**
 * A deliberately small Markdown subset for model replies: paragraphs, line
 * breaks, bullet and numbered lists, **bold**, *italic* and `code` (no underscore emphasis).
 *
 * It builds React elements only (never dangerouslySetInnerHTML), so any HTML in
 * a reply is shown as literal text. Links are not rendered: a model reply cannot
 * create a clickable URL.
 */

// Markers count only at word boundaries: NOT_CHECKED_IN and 2*3*4 stay literal.
const INLINE =
  /((?<![\w*])\*\*(?=\S)[^*\n]+?(?<=\S)\*\*(?![\w*])|`[^`\n]+`|(?<![\w*])\*(?=[^*\s])[^*\n]*?[^*\s]\*(?![\w*])|(?<![\w*])\*[^*\s]\*(?![\w*]))/g;

function inline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (part.length > 2 && part.startsWith("*") && part.endsWith("*")) {
      return <em key={key}>{part.slice(1, -1)}</em>;
    }
    return <Fragment key={key}>{part}</Fragment>;
  });
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/;
// An indented line right after a list item continues that item, as models often write them.
const CONTINUATION = /^\s{2,}\S/;

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[][]; start: number };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.replace(/^#{1,6}\s+/, ""); // headings render as plain emphasis-free text
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    const last = out[out.length - 1];
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      const item = bullet ? bullet[1] : numbered![2];
      if (last && last.kind === kind) last.items.push([item]);
      // A numbered list split by a blank line or a note keeps its own numbers.
      else out.push({ kind, items: [[item]], start: numbered ? Number(numbered[1]) : 1 });
    } else if (last && last.kind !== "p" && CONTINUATION.test(line)) {
      last.items[last.items.length - 1].push(line.trim());
    } else if (line.trim() === "") {
      if (last && last.kind === "p" && last.lines.length) out.push({ kind: "p", lines: [] });
    } else if (last && last.kind === "p") {
      last.lines.push(line);
    } else {
      out.push({ kind: "p", lines: [line] });
    }
  }
  return out.filter((b) => (b.kind === "p" ? b.lines.length > 0 : b.items.length > 0));
}

function Lines({ lines, keyPrefix }: { lines: string[]; keyPrefix: string }) {
  return (
    <>
      {lines.map((line, l) => (
        <Fragment key={l}>
          {l > 0 && <br />}
          {inline(line, `${keyPrefix}-${l}`)}
        </Fragment>
      ))}
    </>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <>
      {blocks(text).map((block, b) => {
        if (block.kind === "p") {
          return (
            <p key={b}>
              <Lines lines={block.lines} keyPrefix={`${b}`} />
            </p>
          );
        }
        const items = block.items.map((lines, i) => (
          <li key={i}>
            <Lines lines={lines} keyPrefix={`${b}-${i}`} />
          </li>
        ));
        return block.kind === "ol" ? (
          <ol key={b} start={block.start === 1 ? undefined : block.start}>
            {items}
          </ol>
        ) : (
          <ul key={b}>{items}</ul>
        );
      })}
    </>
  );
}

/** A short, stable display form of a long record id; the full id stays available. */
export function shortId(id: string): string {
  const match = /^([a-z]+)_([0-9a-f]{8})[0-9a-f]{16,}$/.exec(id);
  return match ? `${match[1]}_${match[2]}…` : id;
}
