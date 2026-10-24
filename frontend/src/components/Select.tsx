import { Children, isValidElement, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactElement, type SelectHTMLAttributes } from 'react'
import { createPortal } from 'react-dom'

type Props = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange' | 'value' | 'defaultValue' | 'multiple'> & {
  value?: string | number
  defaultValue?: string | number
  onValueChange?: (value: string) => void
}
type Option = { value: string; label: string; disabled: boolean }

export default function Select({ children, className = '', style, onValueChange, ...props }: Props) {
  const generatedId = useId()
  const id = props.id ?? generatedId
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const search = useRef({ text: '', time: 0 })
  const [open, setOpen] = useState(false)
  const [fieldLabel, setFieldLabel] = useState<string>()
  const [active, setActive] = useState(0)
  const [uncontrolledValue, setUncontrolledValue] = useState(String(props.defaultValue ?? ''))
  const [position, setPosition] = useState<CSSProperties | null>(null)
  const options: Option[] = Children.toArray(children).filter(isValidElement).map((child) => {
    const option = child as ReactElement<{ value?: string | number; children?: React.ReactNode; disabled?: boolean }>
    const label = Children.toArray(option.props.children).join('')
    return { value: String(option.props.value ?? label), label, disabled: !!option.props.disabled }
  })
  const value = String(props.value ?? uncontrolledValue)
  const selected = Math.max(0, options.findIndex((option) => option.value === value))

  const measureMenu = (): CSSProperties | null => {
    const rect = trigger.current?.getBoundingClientRect()
    if (!rect) return null
    const below = window.innerHeight - rect.bottom - 12
    const above = rect.top - 12
    const up = below < 180 && above > below
    const width = Math.min(Math.max(rect.width, 220), window.innerWidth - 16)
    return {
      position: 'fixed', left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)), width,
      maxHeight: Math.max(60, Math.min(300, up ? above : below)),
      ...(up ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
    }
  }
  const show = () => {
    const next = measureMenu()
    if (!next || props.disabled) return
    // Position the portal before mounting it so it never participates in the
    // document flow or changes the page's scroll height, even for long lists.
    setPosition(next); setActive(selected); setOpen(true)
  }
  const choose = (index: number) => {
    const option = options[index]
    if (!option || option.disabled) return
    if (option.value !== value) onValueChange?.(option.value)
    setUncontrolledValue(option.value); setOpen(false)
    trigger.current?.focus({ preventScroll: true })
  }

  useLayoutEffect(() => {
    const label = trigger.current?.closest('label')
    if (label) setFieldLabel(Array.from(label.childNodes).filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim())
  }, [])
  useLayoutEffect(() => {
    if (!open) return
    const reposition = (event?: Event) => {
      if (event?.target instanceof Node && menu.current?.contains(event.target)) return
      const rect = trigger.current?.getBoundingClientRect()
      if (!rect || rect.bottom < 0 || rect.top > window.innerHeight) { setOpen(false); return }
      setPosition(measureMenu())
    }
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) setOpen(false)
    }
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    document.addEventListener('pointerdown', outside)
    return () => {
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
      document.removeEventListener('pointerdown', outside)
    }
  }, [open])
  useLayoutEffect(() => {
    const list = menu.current
    const option = list?.querySelector<HTMLElement>('[data-active="true"]')
    if (!open || !list || !option) return
    // Scroll only the menu; scrollIntoView would also move its document.
    const top = option.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop
    if (top < list.scrollTop + 6) list.scrollTop = top - 6
    else if (top + option.offsetHeight > list.scrollTop + list.clientHeight - 6) list.scrollTop = top + option.offsetHeight - list.clientHeight + 6
  }, [open, active, position])

  return (
    <div className={`select-control ${className}`} style={style}>
      {props.name && <input type="hidden" name={props.name} form={props.form} value={options[selected]?.value ?? ''} disabled={props.disabled} />}
      <button ref={trigger} id={id} type="button" className="select-trigger" role="combobox" disabled={props.disabled}
        autoFocus={props.autoFocus} tabIndex={props.tabIndex} title={props.title ?? options[selected]?.label}
        aria-label={props['aria-label'] ?? fieldLabel} aria-labelledby={props['aria-labelledby']} aria-describedby={props['aria-describedby']}
        aria-invalid={props['aria-invalid']} aria-expanded={open} aria-haspopup="listbox"
        aria-controls={open ? `${generatedId}-menu` : undefined} aria-activedescendant={open ? `${generatedId}-${active}` : undefined}
        aria-required={props.required} onBlur={(event) => { if (event.relatedTarget && !menu.current?.contains(event.relatedTarget as Node)) setOpen(false) }}
        onClick={() => open ? setOpen(false) : show()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); setOpen(false); return }
          if (event.key === 'Tab') { setOpen(false); return }
          if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
            event.preventDefault()
            const enabled = options.map((option, index) => option.disabled ? -1 : index).filter(index => index >= 0)
            if (!enabled.length) return
            if (!open) { show(); return }
            const current = enabled.indexOf(active)
            setActive(event.key === 'Home' ? enabled[0] : event.key === 'End' ? enabled[enabled.length - 1]
              : enabled[Math.max(0, Math.min(enabled.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)))])
            return
          }
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (open) choose(active); else show(); return }
          if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault()
            const now = Date.now()
            search.current = { text: (now - search.current.time < 650 ? search.current.text : '') + event.key.toLocaleLowerCase(), time: now }
            const found = options.findIndex(option => !option.disabled && option.label.toLocaleLowerCase().startsWith(search.current.text))
            if (found >= 0) { if (!open) show(); setActive(found) }
          }
        }}><span>{options[selected]?.label ?? ''}</span><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="m3 6 5 5 5-5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg></button>
      {open && position && createPortal(<div ref={menu} id={`${generatedId}-menu`} className="select-menu" role="listbox"
        aria-label={props['aria-label'] ?? fieldLabel} style={position}>
        {options.map((option, index) => <div key={option.value} id={`${generatedId}-${index}`} role="option" aria-selected={index === selected}
          aria-disabled={option.disabled} data-active={index === active} className="select-option"
          onPointerMove={() => !option.disabled && setActive(index)} onMouseDown={(event) => event.preventDefault()}
          onClick={() => choose(index)}><span>{option.label}</span>{index === selected && <svg className="select-check" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3 3 7-7" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>}</div>)}
      </div>, document.body)}
    </div>
  )
}
