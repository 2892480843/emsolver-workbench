import { useEffect, useId, useRef, useState, type MouseEvent } from "react";

type OutlineSection = {
  id: string;
  title: string;
};

const COLLAPSED_SECTION_COUNT = 4;

/** Local navigation follows the rendered document, including asynchronously loaded sections. */
export function PageOutline({
  pageKey,
  title = "本页索引",
}: {
  pageKey: string;
  title?: string;
}) {
  const navRef = useRef<HTMLElement>(null);
  const labelId = useId();
  const listId = useId();
  const [sections, setSections] = useState<OutlineSection[]>([]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const main = navRef.current?.closest("main");
    if (!main) return;

    let frame: number | null = null;
    let disposed = false;
    const assignedIds = new Map<HTMLHeadingElement, string>();
    const assignedTabIndexes = new Set<HTMLHeadingElement>();
    const pageSlug = pageKey.replace(/[^a-zA-Z0-9_-]/g, "-") || "document";

    const collect = () => {
      if (disposed) return;
      const next = Array.from(main.querySelectorAll<HTMLHeadingElement>("h2"))
        .map((heading, index) => {
          const headingTitle = heading.textContent?.replace(/\s+/g, " ").trim();
          const visibility = window.getComputedStyle(heading).visibility;
          if (
            !headingTitle ||
            heading.closest('[hidden], [aria-hidden="true"]') ||
            heading.getClientRects().length === 0 ||
            visibility === "hidden" ||
            visibility === "collapse"
          ) {
            return null;
          }

          if (!heading.id) {
            const baseId = `paper-section-${pageSlug}-${index + 1}`;
            let candidate = baseId;
            let suffix = 2;
            while (main.ownerDocument.getElementById(candidate)) {
              candidate = `${baseId}-${suffix++}`;
            }
            heading.id = candidate;
            assignedIds.set(heading, candidate);
          }
          if (!heading.hasAttribute("tabindex")) {
            heading.tabIndex = -1;
            assignedTabIndexes.add(heading);
          }
          return { id: heading.id, title: headingTitle };
        })
        .filter((section): section is OutlineSection => section !== null);

      // The outline's own render is observed too; identical data must not trigger another render.
      setSections((previous) =>
        previous.length === next.length &&
        previous.every(
          (section, index) =>
            section.id === next[index].id &&
            section.title === next[index].title,
        )
          ? previous
          : next,
      );
    };

    const observer = new MutationObserver((mutations) => {
      if (
        frame !== null ||
        mutations.every((mutation) => navRef.current?.contains(mutation.target))
      ) {
        return;
      }
      frame = window.requestAnimationFrame(() => {
        frame = null;
        collect();
      });
    });
    collect();
    observer.observe(main, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["open", "hidden", "aria-hidden"],
    });
    setExpanded(false);

    return () => {
      disposed = true;
      observer.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
      // Restore only attributes owned by this outline; preserve IDs supplied by the page.
      assignedIds.forEach((id, heading) => {
        if (heading.id === id) heading.removeAttribute("id");
      });
      assignedTabIndexes.forEach((heading) => {
        if (heading.getAttribute("tabindex") === "-1") {
          heading.removeAttribute("tabindex");
        }
      });
    };
  }, [pageKey]);

  const navigate = (event: MouseEvent<HTMLAnchorElement>, id: string) => {
    // Preserve native modified clicks. Hashes do not restore the workspace in a new tab;
    // workspace selection currently lives in application state rather than a page router.
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    const main = navRef.current?.closest("main");
    const target = main?.ownerDocument.getElementById(id);
    if (!main || !target || !main.contains(target)) return;
    event.preventDefault();
    target.scrollIntoView({ block: "start", behavior: "auto" });
    target.focus({ preventScroll: true });
  };

  const visibleSections = expanded
    ? sections
    : sections.slice(0, COLLAPSED_SECTION_COUNT);

  return (
    <nav ref={navRef} className="page-outline" aria-labelledby={labelId}>
      <div className="page-outline-heading">
        <strong id={labelId}>{title}</strong>
        <span className="page-outline-count">共 {sections.length} 节</span>
      </div>
      {sections.length ? (
        <ol id={listId} className="page-outline-links">
          {visibleSections.map((section, index) => (
            <li key={section.id}>
              <a
                href={`#${encodeURIComponent(section.id)}`}
                onClick={(event) => navigate(event, section.id)}
              >
                <span className="page-outline-number" aria-hidden="true">
                  {index + 1}.
                </span>
                <span>{section.title}</span>
              </a>
            </li>
          ))}
        </ol>
      ) : (
        <p className="page-outline-empty">当前页面暂无章节</p>
      )}
      {sections.length > COLLAPSED_SECTION_COUNT ? (
        <button
          className="page-outline-toggle"
          type="button"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起索引" : `展开全部 ${sections.length} 节`}
        </button>
      ) : null}
    </nav>
  );
}
