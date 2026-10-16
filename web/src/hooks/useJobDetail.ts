import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Entity } from "../api";
import { emptyDetail, readDetailCycle } from "../models/jobs";
import { errorMessage } from "./useWorkspaceData";

export function useJobDetail(jobId: string) {
  const [state, setState] = useState(() => emptyDetail(""));
  const stateRef = useRef(state);
  const requestRef = useRef<AbortController | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    requestRef.current?.abort();
    setRevision((value) => value + 1);
  }, []);
  // POST responses can show a newly submitted job immediately, even outside the loaded page.
  const setSelectedJob = useCallback((job: Entity) => {
    if (stateRef.current.id !== job.id) requestRef.current?.abort();
    stateRef.current =
      stateRef.current.id === job.id
        ? { ...stateRef.current, job }
        : emptyDetail(job.id, job);
    setState(stateRef.current);
  }, []);
  const updateSelectedJob = useCallback((job: Entity) => {
    if (stateRef.current.id !== job.id) return;
    stateRef.current = { ...stateRef.current, job };
    setState(stateRef.current);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    requestRef.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    if (stateRef.current.id !== jobId) {
      stateRef.current = emptyDetail(jobId);
      setState(stateRef.current);
    }
    setError("");
    const read = async () => {
      let delay: number | null = 2500;
      try {
        const next = await readDetailCycle(
          jobId,
          stateRef.current,
          api,
          controller.signal,
        );
        if (controller.signal.aborted || stateRef.current.id !== jobId) return;
        stateRef.current = next.state;
        setState(next.state);
        setError("");
        delay = next.delay;
      } catch (failure) {
        if (!controller.signal.aborted && stateRef.current.id === jobId)
          setError(errorMessage(failure));
      }
      if (!controller.signal.aborted && delay !== null)
        timer = setTimeout(read, delay);
    };
    if (jobId) void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobId, revision]);

  const visible = state.id === jobId ? state : emptyDetail(jobId);
  return {
    selectedJob: visible.job,
    events: visible.events,
    result: visible.result,
    truncated: visible.truncated,
    error,
    refresh,
    setSelectedJob,
    updateSelectedJob,
  };
}
