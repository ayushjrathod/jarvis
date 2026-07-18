// Widget registry — a new life area adds its widget file plus ONE line here.
import AgentMonitor from "./AgentMonitor.jsx";
import Automations from "./Automations.jsx";
import Brief from "./Brief.jsx";
import CommandBox from "./CommandBox.jsx";
import Stats from "./Stats.jsx";
import Tasks from "./Tasks.jsx";

export const widgets = [
  { id: "command", title: "Command", area: null, Component: CommandBox },
  { id: "brief", title: "Today's brief", area: "tasks", Component: Brief },
  { id: "tasks", title: "Tasks", area: "tasks", Component: Tasks },
  { id: "agents", title: "Agent activity", area: null, Component: AgentMonitor },
  { id: "automations", title: "Automations", area: null, Component: Automations },
  { id: "stats", title: "Observability", area: null, Component: Stats },
];
