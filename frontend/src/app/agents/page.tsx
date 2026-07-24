import ConnectorConsole from '@/components/ConnectorConsole';
import HostedAgentCreator from '@/components/HostedAgentCreator';

export default function AgentsPage() {
  return (
    <div className="mx-auto max-w-7xl px-4 py-12">
      <ConnectorConsole />
      <HostedAgentCreator />
    </div>
  );
}
