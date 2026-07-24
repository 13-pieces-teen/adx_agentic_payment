import Image from 'next/image';
import Link from 'next/link';
import ConnectorConsole from '@/components/ConnectorConsole';
import HostedAgentCreator from '@/components/HostedAgentCreator';

export default function AgentsPage() {
  return (
    <div className="site-main">
      <section className="page-head">
        <Link className="back-btn" href="/">
          ← Back
        </Link>
        <div className="page-head-art">
          <div>
            <p className="label">#2 Deploy</p>
            <h1 className="display page-title">Agents</h1>
            <p className="sec-sub">
              Bring a local runtime or create a Hosted Agent. Both enter the same
              Arena task and result boundary.
            </p>
          </div>
          <Image
            src="/img/art-agents.webp"
            alt="Engraving of a hand moving a chess pawn"
            width={800}
            height={520}
            priority
          />
        </div>
      </section>

      <section className="section agent-workbench">
        <div className="workbench-label">
          <p className="label">Local Piece</p>
          <h2 className="display">Connector Workshop</h2>
          <p className="sec-sub">
            Pair one outbound connection, inspect detected runtimes, and bind only
            the capabilities you intend to use.
          </p>
        </div>
        <ConnectorConsole />
        <HostedAgentCreator />
      </section>
    </div>
  );
}
