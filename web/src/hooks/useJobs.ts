import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  applyJobPage,
  jobPagePath,
  listPollDelay,
  type JobList,
  type JobPage,
  type WorkspaceSummary,
} from "../models/jobs";
import { errorMessage } from "./useWorkspaceData";

type ListState = {
  scope: string;
  list: JobList | null;
  summary: WorkspaceSummary | null;
};

export function useJobs(projectId: string, status: string, enabled = true) {
  const scope = `${projectId}\0${status}`;
  const [state, setState] = useState<ListState>({
    scope,
    list: null,
    summary: null,
  });
  const stateRef = useRef(state);
  const controllerRef = useRef<AbortController | null>(null);
  const moreRequest = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    // A user refresh starts a new server cursor snapshot, including newly inserted jobs.
    controllerRef.current?.abort();
    stateRef.current = { ...stateRef.current, list: null };
    setState(stateRef.current);
    setRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    if (!enabled) {
      stateRef.current = { scope, list: null, summary: null };
      setState(stateRef.current);
      setLoading(false);
      setLoadingMore(false);
      setError("");
      return () => controller.abort();
    }
    if (stateRef.current.scope !== scope) {
      stateRef.current = { scope, list: null, summary: null };
      setState(stateRef.current);
    }
    setLoading(true);
    setLoadingMore(false);
    setError("");
    const poll = async () => {
      const summaryPath = `/workspace/summary${projectId ? `?${new URLSearchParams({ project_id: projectId })}` : ""}`;
      const [page, summary] = await Promise.allSettled([
        api<JobPage>(jobPagePath(projectId, status), {
          signal: controller.signal,
        }),
        api<WorkspaceSummary>(summaryPath, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      const current = stateRef.current;
      stateRef.current = {
        scope,
        list:
          page.status === "fulfilled"
            ? applyJobPage(
                current.list,
                page.value,
                "refresh",
                projectId,
                status,
              )
            : current.list,
        summary:
          summary.status === "fulfilled" ? summary.value : current.summary,
      };
      setState(stateRef.current);
      setError(
        [page, summary]
          .flatMap((value) =>
            value.status === "rejected" ? [errorMessage(value.reason)] : [],
          )
          .join("；"),
      );
      setLoading(false);
      timer = setTimeout(
        poll,
        listPollDelay(stateRef.current.summary?.active_jobs ?? 1),
      );
    };
    void poll();
    return () => {
      controller.abort();
      moreRequest.current?.abort();
      moreRequest.current = null;
      clearTimeout(timer);
    };
  }, [projectId, status, scope, revision, enabled]);

  const loadMore = useCallback(async () => {
    const controller = controllerRef.current;
    const cursor =
      stateRef.current.scope === scope
        ? stateRef.current.list?.next_cursor
        : null;
    if (
      !cursor ||
      !controller ||
      controller.signal.aborted ||
      moreRequest.current
    )
      return;
    moreRequest.current = controller;
    setLoadingMore(true);
    try {
      const page = await api<JobPage>(jobPagePath(projectId, status, cursor), {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      stateRef.current = {
        ...stateRef.current,
        list: applyJobPage(
          stateRef.current.list,
          page,
          "append",
          projectId,
          status,
        ),
      };
      setState(stateRef.current);
      setError("");
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (moreRequest.current === controller) moreRequest.current = null;
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }, [projectId, status, scope]);

  const visible = enabled && state.scope === scope ? state : null;
  return {
    jobs: visible?.list?.items || [],
    summary: visible?.summary || null,
    total: visible?.list?.total,
    hasMore: Boolean(visible?.list?.next_cursor),
    loading,
    loadingMore,
    error,
    refresh,
    loadMore,
  };
}
