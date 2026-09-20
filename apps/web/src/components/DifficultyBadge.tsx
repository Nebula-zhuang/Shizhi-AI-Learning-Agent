interface LevelDotsProps {
  /** 1-5 */
  value: number
  label: string
}

/** 用点阵表示 1-5 的等级。比数字更直观，也更容易一眼比较。 */
export function LevelDots({ value, label }: LevelDotsProps) {
  const clamped = Math.max(1, Math.min(5, value))
  return (
    <span className="inline-flex items-center gap-1.5" title={`${label} ${clamped}/5`}>
      <span className="text-xs text-ink-3">{label}</span>
      <span className="inline-flex gap-0.5">
        {[1, 2, 3, 4, 5].map((i) => (
          <span
            key={i}
            className={[
              'h-1.5 w-1.5 rounded-full',
              i <= clamped ? 'bg-moss' : 'bg-line',
            ].join(' ')}
          />
        ))}
      </span>
      <span className="text-xs text-ink-4">{clamped}</span>
    </span>
  )
}

interface DifficultyBadgeProps {
  difficulty: number
  importance: number
}

/** 难度 + 重要度。两者一起展示，学生才能判断"该先看哪个"。 */
export function DifficultyBadge({ difficulty, importance }: DifficultyBadgeProps) {
  return (
    <span className="inline-flex flex-wrap items-center gap-3">
      <LevelDots value={difficulty} label="难度" />
      <LevelDots value={importance} label="重要度" />
    </span>
  )
}
