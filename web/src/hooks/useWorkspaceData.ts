import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Entity } from "../api";

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export function useWorkspaceData() {
  const [projects, setProjects] = useState<Entity[]>([]);
  const [matrices, setMatrices] = useState<Entity[]>([]);
  const [health, setHealth] = useState<Entity | null>(null);
  const [capabilities, setCapabilities] = useState<Entity>({});
  const [identity, setIdentity] = useState<Entity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    const endpoints = [
      "/projects",
      "/matrices",
      "/capabilities",
      "/health",
      "/identity",
    ];
    const apply = [
      setProjects,
      setMatrices,
      setCapabilities,
      setHealth,
      setIdentity,
    ];
    const settled = await Promise.allSettled(
      endpoints.map((path) => api<any>(path, { signal: controller.signal })),
    );
    if (controller.signal.aborted) return;
    const failures: string[] = [];
    settled.forEach((item, index) => {
      if (item.status === "fulfilled") apply[index](item.value);
      else failures.push(`${endpoints[index]}: ${errorMessage(item.reason)}`);
    });
    setError(failures.join("；"));
    setLoading(false);
  }, []);

  useEffect(() => {
    void reload();
    return () => request.current?.abort();
  }, [reload]);

  return {
    projects,
    setProjects,
    matrices,
    setMatrices,
    health,
    capabilities,
    identity,
    loading,
    error,
    reload,
  };
}
