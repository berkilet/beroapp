import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';

export const metadata: Metadata = {
  title: 'beroapp — prediction-market research',
  description: 'Self-hosted Polymarket prediction intelligence and shadow-trading research platform',
};

const NAV = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/markets', label: 'Markets' },
  { href: '/opportunities', label: 'Opportunities' },
  { href: '/predictions', label: 'Predictions' },
  { href: '/paper-trading', label: 'Paper Trading' },
  { href: '/portfolio', label: 'Portfolio' },
  { href: '/performance', label: 'Performance' },
  { href: '/calibration', label: 'Calibration' },
  { href: '/model-health', label: 'Model Health' },
  { href: '/data-sources', label: 'Data Sources' },
  { href: '/system', label: 'System' },
  { href: '/audit', label: 'Audit' },
  { href: '/settings', label: 'Settings' },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-surface text-gray-200 antialiased">
        <div className="flex min-h-screen">
          <nav className="w-52 shrink-0 border-r border-edge bg-panel">
            <div className="border-b border-edge px-4 py-4">
              <div className="font-mono text-sm font-semibold text-gray-100">beroapp</div>
              <div className="mt-0.5 text-[11px] text-muted">research platform</div>
            </div>
            <ul className="py-2">
              {NAV.map((item) => (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    className="block px-4 py-1.5 text-xs text-gray-400 transition hover:bg-edge/40 hover:text-gray-100"
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
            {/* Always visible, on every page. The single most important fact
                about this system is that it is not trading. */}
            <div className="mx-3 mt-4 rounded border border-ok/30 bg-ok/10 px-3 py-2">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-ok">
                Live trading
              </div>
              <div className="mt-0.5 font-mono text-xs text-ok">DISABLED</div>
              <div className="mt-1 text-[10px] leading-snug text-muted">
                No real-money order can be placed. Any capital figure shown is virtual.
              </div>
            </div>
          </nav>
          <main className="min-w-0 flex-1 p-6">{children}</main>
        </div>
      </body>
    </html>
  );
}
