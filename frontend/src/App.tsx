import { useEffect, useState } from "react";

import { PileLanding } from "./components/PileLanding";
import { PileWorkspace } from "./components/PileWorkspace";
import { api } from "./lib/api";
import { loadRecentPiles, rememberPile, type RecentPile } from "./lib/recentPiles";
import type { Pile } from "./lib/types";

export default function App() {
  const [pile, setPile] = useState<Pile | null>(null);
  const [recentPiles, setRecentPiles] = useState<RecentPile[]>(() => loadRecentPiles());
  const [healthy, setHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    void api.health().then(setHealthy).catch(() => setHealthy(false));
  }, []);

  function openPile(next: Pile) {
    setPile(next);
    setRecentPiles(rememberPile(next));
  }

  return (
    <>
      <div className={`health-indicator ${healthy === true ? "healthy" : healthy === false ? "unhealthy" : "checking"}`} title="Backend health">
        <span /> {healthy === true ? "API connected" : healthy === false ? "API unavailable" : "Checking API"}
      </div>
      {pile ? <PileWorkspace onBack={() => setPile(null)} pile={pile} /> : <PileLanding onOpen={openPile} recentPiles={recentPiles} />}
    </>
  );
}
