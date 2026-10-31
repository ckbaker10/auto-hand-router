// Uses the pinned published package; no upstream solver changes.
import fs from "node:fs"
import { pathToFileURL } from "node:url"
const [input, packageFile, output] = process.argv.slice(2)
const { AutoroutingPipelineSolver } = await import(pathToFileURL(packageFile))
const d = JSON.parse(fs.readFileSync(input, "utf8"))
const layers = ["top", "bottom"]
const rules = Object.values(d.rules)
const max = key => Math.max(...rules.map(r => r[key]))
const xs = d.outline.map(p => p[0]), ys = d.outline.map(p => p[1])
const srj = {
  layerCount: 2, minTraceWidth: Math.min(...rules.map(r => r.width)),
  nominalTraceWidth: Math.min(...rules.map(r => r.width)),
  defaultObstacleMargin: max("clearance"), minTraceToPadEdgeClearance: max("clearance"),
  minTraceToHoleEdgeClearance: max("hole"), minBoardEdgeClearance: max("edge"),
  minViaEdgeToPadEdgeClearance: max("clearance"),
  minViaHoleEdgeToViaHoleEdgeClearance: max("hole_to_hole"),
  minPlatedHoleDrillEdgeToDrillEdgeClearance: max("hole_to_hole"),
  minViaPadDiameter: max("via_diameter"), minViaHoleDiameter: max("via_drill"),
  allowViaInPad: false, allowBlindAndBuriedVias: false, allowJumpers: false,
  bounds: {minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys)},
  outline: d.outline.map(([x,y]) => ({x,y})),
  obstacles: d.pads.map(p => ({
    type: "rect", obstacleId: p.id, componentId: p.component,
    layers: p.layers.map(i => layers[i]), center: {x:p.center[0],y:p.center[1]},
    width: p.bounds[2]-p.bounds[0], height:p.bounds[3]-p.bounds[1],
    connectedTo: p.plated && p.net ? [d.nets[p.net]] : [],
    ...(!p.plated ? {isNonPlatedHole:true,shape:"circle"} : {})
  })),
  connections: Object.entries(d.nets).filter(([n]) => Number(n)>0).map(([n,name]) => ({
    name, nominalTraceWidth:d.rules[n].width,
    pointsToConnect:d.pads.filter(p => p.plated && String(p.net)===n).map(p => ({
      x:p.center[0],y:p.center[1], layers:p.layers.map(i => layers[i]), pointId:p.id
    }))
  }))
}
fs.writeFileSync(output+".input.json", JSON.stringify(srj,null,2))
const start=Date.now()
const solver = new AutoroutingPipelineSolver(srj)
while (!solver.solved && !solver.failed) solver.step()
if (solver.failed) throw new Error(solver.error)
const result=solver.getOutputSimpleRouteJson()
fs.writeFileSync(output+".raw.json",JSON.stringify(result,null,2))
const tracks=[], vias=[]
for (const trace of result.traces ?? []) {
  let previous
  for (const point of trace.route) {
    if (point.route_type === "wire") {
      if (previous && previous.layer === point.layer && (previous.x !== point.x || previous.y !== point.y)) {
        tracks.push({net:trace.connection_name,layer:layers.indexOf(point.layer),width:point.width,
          start:[previous.x,previous.y],end:[point.x,point.y]})
      }
      previous=point
    } else if (point.route_type === "via") {
      vias.push({net:trace.connection_name,center:[point.x,point.y],
        diameter:point.via_diameter ?? srj.minViaPadDiameter,
        drill:point.via_hole_diameter ?? srj.minViaHoleDiameter})
      // A transition is a copper endpoint on both sides of the barrel.
      previous={x:point.x,y:point.y,layer:point.to_layer}
    } else if (point.route_type === "through_obstacle") {
      previous={...point.end,layer:point.to_layer}
    } else throw new Error("Unsupported route primitive: "+point.route_type)
  }
}
// Several trace records can share one drilled barrel at a same-net junction.
// KiCad has one physical via there, so deduplicate identical copper geometry.
const uniqueVias=[...new Map(vias.map(v => [JSON.stringify(v),v])).values()]
const elapsed_seconds=(Date.now()-start)/1000
fs.writeFileSync(output,JSON.stringify({tracks,vias:uniqueVias,elapsed_seconds,
  raw_via_entries:vias.length},null,2))
console.log(JSON.stringify({elapsed_seconds,tracks:tracks.length,vias:uniqueVias.length,
  raw_via_entries:vias.length}))
