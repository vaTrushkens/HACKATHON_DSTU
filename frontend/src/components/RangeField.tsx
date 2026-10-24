import { useId, useRef, type CSSProperties } from 'react'

type Props = {
  label: string
  value: number
  min: number
  max: number
  step?: number
  displayValue: string
  ticks: readonly (string | { value: number; label: string })[]
  onChange: (value: number) => void
  onCommit?: (value: number) => void
}

export default function RangeField({ label, value, min, max, step = 1, displayValue, ticks, onChange, onCommit }: Props) {
  const id = useId()
  const interaction = useRef({ pointer: false, keyboard: false, dirty: false })
  const commit = (input: HTMLInputElement) => {
    if (interaction.current.dirty) { interaction.current.dirty = false; onCommit?.(Number(input.value)) }
  }
  const progress = Math.max(0, Math.min(100, (value - min) / (max - min) * 100))
  return (
    <div className="range-field" style={{ '--range-progress': `${progress}%` } as CSSProperties}>
      <div className="range-field-box">
        <div className="range-field-head">
          <label htmlFor={id}>{label}</label>
          <output htmlFor={id}>{displayValue}</output>
        </div>
        <input id={id} className="range-input" type="range" min={min} max={max} step={step} value={value}
          aria-valuetext={displayValue}
          onChange={(event) => {
            interaction.current.dirty = true
            onChange(Number(event.target.value))
            if (!interaction.current.pointer && !interaction.current.keyboard) commit(event.currentTarget)
          }}
          onPointerDown={(event) => { interaction.current.pointer = true; event.currentTarget.setPointerCapture(event.pointerId) }}
          onPointerUp={(event) => { interaction.current.pointer = false; commit(event.currentTarget) }}
          onPointerCancel={(event) => { interaction.current.pointer = false; commit(event.currentTarget) }}
          onLostPointerCapture={(event) => { interaction.current.pointer = false; commit(event.currentTarget) }}
          onKeyDown={(event) => { if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) interaction.current.keyboard = true }}
          onKeyUp={(event) => { if (interaction.current.keyboard) { interaction.current.keyboard = false; commit(event.currentTarget) } }}
          onBlur={(event) => { interaction.current.pointer = false; interaction.current.keyboard = false; commit(event.currentTarget) }} />
      </div>
      <div className="range-ticks" aria-hidden="true">{ticks.map((tick, index) => <span key={index} style={{ left: `${typeof tick === 'string' ? index / (ticks.length - 1) * 100 : (tick.value - min) / (max - min) * 100}%` }}>{typeof tick === 'string' ? tick : tick.label}</span>)}</div>
    </div>
  )
}
