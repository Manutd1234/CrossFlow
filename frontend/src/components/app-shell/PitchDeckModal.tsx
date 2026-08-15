import React, { useEffect, useId, useRef, useState } from 'react';
import { Award, ChevronLeft, ChevronRight, Cpu, Layers, Leaf, Presentation, ShieldCheck, X } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';

interface PitchDeckModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const PitchDeckModal: React.FC<PitchDeckModalProps> = ({ isOpen, onClose }) => {
  const [slide, setSlide] = useState<1 | 2>(1);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (!isOpen) return;

    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const dialog = dialogRef.current;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }

      if (event.key === 'ArrowLeft' || event.key === 'Home') {
        event.preventDefault();
        setSlide(1);
        return;
      }

      if (event.key === 'ArrowRight' || event.key === 'End') {
        event.preventDefault();
        setSlide(2);
        return;
      }

      if (event.key !== 'Tab' || !dialog) return;

      const focusableElements = Array.from(dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));

      if (focusableElements.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }

      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];
      const activeElement = document.activeElement;
      const focusIsOutside = !activeElement || !dialog.contains(activeElement);

      if (event.shiftKey && (activeElement === firstElement || focusIsOutside)) {
        event.preventDefault();
        lastElement.focus();
      } else if (!event.shiftKey && (activeElement === lastElement || focusIsOutside)) {
        event.preventDefault();
        firstElement.focus();
      }
    };

    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      role="presentation"
      onMouseDown={event => {
        if (event.target === event.currentTarget) onClose();
      }}
      style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      zIndex: 9999,
      background: 'rgba(15, 23, 42, 0.45)',
      backdropFilter: 'blur(10px)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '10px clamp(10px, 3vw, 24px)'
    }}>
      <div
        ref={dialogRef}
        className="glass-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        style={{
        width: 'min(1000px, 100%)',
        height: 'min(680px, calc(100dvh - 20px))',
        display: 'flex',
        flexDirection: 'column',
        position: 'relative',
        overflow: 'hidden',
        background: 'var(--bg-secondary)',
        border: '1px solid var(--border-accent)',
        boxShadow: '0 30px 70px -20px rgba(15, 23, 42, 0.45)'
        }}
      >
        {/* Header Bar */}
        <div style={{
          padding: 'clamp(12px, 2.5vw, 16px) clamp(14px, 3vw, 24px)',
          borderBottom: '1px solid var(--border-color)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '12px',
          flexWrap: 'wrap',
          flexShrink: 0,
          background: 'var(--surface-2)'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
            <Presentation aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-cyan)" />
            <div>
              <h2 id={titleId} style={{ fontSize: 'clamp(0.88rem, 2vw, 1rem)', color: 'var(--text-primary)', margin: 0 }}>
                CrossFlow AI • Batam-Singapore Stage Presentation
              </h2>
              <p id={descriptionId} style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                Two-slide overview. Use the arrow keys or controls below to navigate.
              </p>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
            <span aria-live="polite" style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
              Slide {slide} of 2
            </span>
            <button
              ref={closeButtonRef}
              type="button"
              aria-label="Close presentation"
              onClick={onClose}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--text-secondary)',
                cursor: 'pointer',
                width: '40px',
                height: '40px',
                borderRadius: '10px',
                display: 'grid',
                placeItems: 'center'
              }}
            >
              <X size={ICON_SIZE.big} aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Slide Canvas Body */}
        <section
          role="document"
          aria-live="polite"
          aria-label={`Slide ${slide} of 2`}
          style={{ flex: 1, minHeight: 0, padding: 'clamp(10px, 2.4vh, 22px) clamp(12px, 3vw, 28px)', overflow: 'hidden', display: 'flex', flexDirection: 'column', justifyContent: 'flex-start' }}
        >
          {slide === 1 ? (
            /* SLIDE 1: PROBLEM & REGIONAL OPPORTUNITY */
            <div style={{ minHeight: 0, display: 'flex', flex: 1, flexDirection: 'column', gap: 'clamp(8px, 1.8vh, 16px)' }}>
              <span className="badge badge-smooth" style={{ width: 'fit-content' }}>
                SLIDE 1 • PROBLEM & REGIONAL CHALLENGE
              </span>

              <h3 style={{ margin: 0, color: 'var(--text-primary)', fontSize: 'clamp(1.15rem, 3vw, 1.85rem)', fontWeight: 800, lineHeight: 1.15 }}>
                Unclogging the Batam-Singapore Corridor Through AI & Synchronized Logistics
              </h3>

              <div style={{ minHeight: 0, display: 'grid', flex: 1, gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 'clamp(10px, 2vw, 16px)' }}>
                <article style={{ background: 'var(--surface-1)', padding: 'clamp(12px, 2vw, 16px)', borderRadius: '14px', border: '1px solid var(--border-color)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.95rem', lineHeight: 1.25, color: 'var(--accent-rose)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <ShieldCheck aria-hidden="true" size={ICON_SIZE.large} /> Bottlenecks & Delays
                  </h4>
                  <p style={{ fontSize: '0.8rem', lineHeight: 1.45, color: 'var(--text-secondary)', marginTop: '8px' }}>
                    The demo models peak-hour pressure around Mukakuning and Simpang Kabil and shows how road delays can threaten ferry connections at Batam Centre.
                  </p>
                </article>

                <article style={{ background: 'var(--surface-1)', padding: 'clamp(12px, 2vw, 16px)', borderRadius: '14px', border: '1px solid var(--border-color)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.95rem', lineHeight: 1.25, color: 'var(--accent-amber)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Layers aria-hidden="true" size={ICON_SIZE.large} /> Siloed Operations
                  </h4>
                  <p style={{ fontSize: '0.8rem', lineHeight: 1.45, color: 'var(--text-secondary)', marginTop: '8px' }}>
                    Road traffic planning, customs clearance buffers, and ferry timetables currently operate in silos without predictive cross-modal intelligence.
                  </p>
                </article>

                <article style={{ background: 'var(--surface-1)', padding: 'clamp(12px, 2vw, 16px)', borderRadius: '14px', border: '1px solid var(--border-color)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.95rem', lineHeight: 1.25, color: 'var(--accent-emerald)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Leaf aria-hidden="true" size={ICON_SIZE.large} /> Carbon & Fuel Waste
                  </h4>
                  <p style={{ fontSize: '0.8rem', lineHeight: 1.45, color: 'var(--text-secondary)', marginTop: '8px' }}>
                    Uncoordinated departure and customs windows can create avoidable idling, fuel use, and emissions for freight vehicles.
                  </p>
                </article>
              </div>
            </div>
          ) : (
            /* SLIDE 2: SOLUTION & ARCHITECTURE */
            <div style={{ minHeight: 0, display: 'flex', flex: 1, flexDirection: 'column', gap: 'clamp(8px, 1.8vh, 16px)' }}>
              <span className="badge badge-smooth" style={{ width: 'fit-content' }}>
                SLIDE 2 • CROSSFLOW AI SOLUTION & ARCHITECTURE
              </span>

              <h3 style={{ margin: 0, color: 'var(--text-primary)', fontSize: 'clamp(1.15rem, 3vw, 1.85rem)', fontWeight: 800, lineHeight: 1.15 }}>
                Predictive AI Forecasting & Cross-Border Logistics Engine
              </h3>

              <div style={{ minHeight: 0, display: 'grid', flex: 1, gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'clamp(10px, 2vw, 16px)' }}>
                <article style={{ background: 'var(--surface-1)', padding: 'clamp(12px, 2vw, 16px)', borderRadius: '14px', border: '1px solid var(--border-color)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.95rem', lineHeight: 1.25, color: 'var(--accent-cyan)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Cpu aria-hidden="true" size={ICON_SIZE.big} /> Core Technical Capabilities
                  </h4>
                  <ul style={{ fontSize: 'clamp(0.72rem, 1.35vw, 0.82rem)', lineHeight: 1.42, color: 'var(--text-secondary)', marginTop: '8px', paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    <li><strong>Real OSM Road Graph + A* Routing:</strong> 84,000-node Batam network; shortest paths via an admissible haversine heuristic, so routes are provably optimal.</li>
                    <li><strong>Random Forest Congestion Model:</strong> 30 and 60 minute forecasts, trained on a synthetic Batam traffic profile pending a live feed.</li>
                    <li><strong>Dynamic Departure Window Solver:</strong> Compares departing now against 30 minutes later and recommends the cheaper window.</li>
                    <li><strong>Ferry Synchronization:</strong> Matches rolling Batam Centre, Sekupang and HarbourBay sailings to road ETAs plus boarding cutoff.</li>
                  </ul>
                </article>

                <article style={{ background: 'var(--surface-1)', padding: 'clamp(12px, 2vw, 16px)', borderRadius: '14px', border: '1px solid var(--border-color)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.95rem', lineHeight: 1.25, color: 'var(--accent-emerald)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Award aria-hidden="true" size={ICON_SIZE.big} /> Measurable Regional Impact
                  </h4>
                  {/* Framed as modelled projections under stated assumptions.
                      These were previously asserted as measured outcomes, which
                      no code path in the repo could substantiate. */}
                  <ul style={{ fontSize: 'clamp(0.72rem, 1.35vw, 0.82rem)', lineHeight: 1.42, color: 'var(--text-secondary)', marginTop: '8px', paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    <li><strong>~540 kg CO2/day avoidable</strong> across five corridors — modelled at 40 advised trips per corridor per hour, 35% of queue delay avoided, 1.8 kg/h idle burn.</li>
                    <li><strong>Departure deferral</strong> shifts trips out of the modelled 08:00 and 18:00 peaks, where the index roughly doubles.</li>
                    <li><strong>Boarding-aware connections:</strong> sailings are only offered if reachable before the 15-minute cutoff.</li>
                  </ul>
                  <p style={{ fontSize: '0.68rem', lineHeight: 1.35, color: 'var(--text-muted)', marginTop: '8px' }}>
                    Projections from a simulated traffic model; assumptions published in the API response.
                  </p>
                </article>
              </div>
            </div>
          )}
        </section>

        {/* Footer Navigation controls */}
        <div style={{
          padding: 'clamp(12px, 2.5vw, 16px) clamp(14px, 3vw, 24px)',
          borderTop: '1px solid var(--border-color)',
          background: 'var(--surface-2)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '12px',
          flexWrap: 'wrap',
          flexShrink: 0
        }}>
          <button
            type="button"
            className="glass-button"
            aria-label="Go to previous presentation slide"
            onClick={() => setSlide(1)}
            disabled={slide === 1}
            style={{ opacity: slide === 1 ? 0.4 : 1 }}
          >
            <ChevronLeft aria-hidden="true" size={ICON_SIZE.large} /> Previous Slide
          </button>

          <div role="group" aria-label="Choose presentation slide" style={{ display: 'flex', gap: '8px' }}>
            {([1, 2] as const).map(slideNumber => (
              <button
                key={slideNumber}
                type="button"
                aria-label={`Go to slide ${slideNumber}`}
                aria-current={slide === slideNumber ? 'step' : undefined}
                onClick={() => setSlide(slideNumber)}
                style={{ width: '28px', height: '28px', border: 0, borderRadius: '50%', background: 'transparent', display: 'grid', placeItems: 'center', cursor: 'pointer' }}
              >
                <span aria-hidden="true" style={{ width: '11px', height: '11px', borderRadius: '50%', background: slide === slideNumber ? 'var(--accent-cyan)' : '#cbd5e1' }} />
              </button>
            ))}
          </div>

          <button
            type="button"
            className="ui-button-primary"
            aria-label="Go to next presentation slide"
            onClick={() => setSlide(2)}
            disabled={slide === 2}
            style={{ opacity: slide === 2 ? 0.4 : 1 }}
          >
            Next Slide <ChevronRight aria-hidden="true" size={ICON_SIZE.large} />
          </button>
        </div>
      </div>
    </div>
  );
};
