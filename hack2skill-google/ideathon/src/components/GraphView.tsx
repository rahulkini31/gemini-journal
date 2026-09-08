import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from "d3-force";
import { useMemo } from "react";

export interface GraphViewNode {
  id: string;
  label: string;
  kind?: string;
}

export interface GraphViewEdge {
  source: string;
  target: string;
  label?: string;
  weight?: number;
}

interface GraphViewProps {
  nodes: GraphViewNode[];
  edges: GraphViewEdge[];
  colorForKind?: (kind: string | undefined) => string;
  emptyMessage: string;
  height?: number;
  onNodeSelect?: (nodeId: string) => void;
  selectedNodeId?: string | null;
}

interface LaidOutNode extends GraphViewNode {
  x: number;
  y: number;
}

const WIDTH = 640;
const NODE_RADIUS = 10;
const SIMULATION_TICKS = 300;

/**
 * A generic force-directed graph, shared by the relationship graph
 * (find_related_reflections) and the emotional-pattern graph
 * (analyze_emotional_patterns) — both return a {nodes, edges} shape, just
 * with different node "kind"s and edge metadata.
 *
 * The simulation is run to a fixed number of ticks synchronously (not
 * animated frame-by-frame) so this component stays a pure function of its
 * props — no refs, no requestAnimationFrame, no imperative DOM handoff to
 * D3 — which keeps it straightforward to drive from Playwright BDD steps
 * without needing to fake animation timing.
 */
export default function GraphView({
  nodes,
  edges,
  colorForKind = () => "var(--graph-node-default, #6366f1)",
  emptyMessage,
  height = 420,
  onNodeSelect,
  selectedNodeId,
}: GraphViewProps) {
  const layout = useMemo(() => computeLayout(nodes, edges, height), [nodes, edges, height]);

  if (nodes.length === 0) {
    return <div className="graph-empty" role="status">{emptyMessage}</div>;
  }

  const byId = new Map<string, LaidOutNode>(layout.map((node) => [node.id, node]));

  return (
    <svg
      className="graph-view"
      viewBox={`0 0 ${WIDTH} ${height}`}
      role="img"
      aria-label="Reflection graph visualization"
      width="100%"
      height={height}
    >
      <g className="graph-edges">
        {edges.map((edge, index) => {
          const source = byId.get(edge.source);
          const target = byId.get(edge.target);
          if (!source || !target) return null;
          return (
            <line
              key={`${edge.source}-${edge.target}-${index}`}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              className="graph-edge"
              data-reason={edge.label}
            >
              <title>{edge.label}</title>
            </line>
          );
        })}
      </g>
      <g className="graph-nodes">
        {layout.map((node) => (
          <g
            key={node.id}
            transform={`translate(${node.x}, ${node.y})`}
            className={node.id === selectedNodeId ? "graph-node selected" : "graph-node"}
          >
            <circle
              r={NODE_RADIUS}
              fill={colorForKind(node.kind)}
              role={onNodeSelect ? "button" : undefined}
              tabIndex={onNodeSelect ? 0 : undefined}
              aria-label={node.label}
              onClick={() => onNodeSelect?.(node.id)}
              onKeyDown={(event) => {
                if (onNodeSelect && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  onNodeSelect(node.id);
                }
              }}
            >
              <title>{node.label}</title>
            </circle>
            <text x={0} y={NODE_RADIUS + 12} textAnchor="middle" className="graph-node-label">
              {truncate(node.label, 24)}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function computeLayout(nodes: GraphViewNode[], edges: GraphViewEdge[], height: number): LaidOutNode[] {
  if (nodes.length === 0) return [];

  type SimNode = GraphViewNode & { x: number; y: number; vx?: number; vy?: number; index?: number };
  const simNodes: SimNode[] = nodes.map((node) => ({ ...node, x: 0, y: 0 }));
  const nodeIds = new Set(simNodes.map((node) => node.id));
  const simLinks = edges
    .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .map((edge) => ({ source: edge.source, target: edge.target }));

  const simulation = forceSimulation(simNodes)
    .force("charge", forceManyBody().strength(-120))
    .force("link", forceLink(simLinks).id((node: any) => node.id).distance(70))
    .force("center", forceCenter(WIDTH / 2, height / 2))
    // Wider than the circle itself (NODE_RADIUS + 8 would suffice for the
    // circles alone) to leave room for each node's label underneath it —
    // found via manual UAT with a small, tightly-linked graph where labels
    // otherwise overlapped illegibly; the CSS text halo (index.css's
    // .graph-node-label) is the other half of this fix, for whatever
    // overlap still occurs in denser graphs.
    .force("collide", forceCollide(NODE_RADIUS + 22))
    .stop();

  const minX = NODE_RADIUS + 20;
  const maxX = WIDTH - NODE_RADIUS - 20;
  const minY = NODE_RADIUS + 20;
  const maxY = height - NODE_RADIUS - 20;

  // Clamping only after the simulation finished (rather than each tick) let
  // the collision/link forces push a node past the visible bounds during
  // the simulation, then squash it back in one step at the very end —
  // distorting its distance from neighbors it was otherwise correctly
  // spaced from mid-simulation (found via manual UAT: an isolated 2-node
  // pair near the edge rendered nearly on top of each other despite a
  // 70px link distance). Clamping every tick instead keeps the forces
  // interacting correctly against the actual boundary throughout.
  //
  // Zeroing the velocity component when a clamp fires (not just the
  // position) matters too: d3-force carries a node's velocity from tick to
  // tick, so a node pinned at the boundary with its pre-clamp velocity
  // intact gets shoved into the same wall again next tick — a small
  // jitter/sticking artifact in denser graphs, caught during /simplify.
  for (let tick = 0; tick < SIMULATION_TICKS; tick += 1) {
    simulation.tick();
    for (const node of simNodes) {
      const clampedX = clamp(node.x, minX, maxX);
      const clampedY = clamp(node.y, minY, maxY);
      if (clampedX !== node.x) node.vx = 0;
      if (clampedY !== node.y) node.vy = 0;
      node.x = clampedX;
      node.y = clampedY;
    }
  }

  return simNodes;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
