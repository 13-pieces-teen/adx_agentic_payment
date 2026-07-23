'use client';

import { useEffect, useState } from 'react';
import { motion, useMotionValue, useTransform, animate } from 'framer-motion';

export default function LiveCounter({
  value,
  isCurrency = false,
}: {
  value: string | number;
  isCurrency?: boolean;
}) {
  const [displayValue, setDisplayValue] = useState(0);
  const count = useMotionValue(0);
  const rounded = useTransform(count, (latest) => Math.round(latest));

  useEffect(() => {
    const target = typeof value === 'string' ? parseFloat(value.replace(/[^0-9.]/g, '')) : value;
    if (isNaN(target)) {
      setDisplayValue(0);
      return;
    }

    const controls = animate(count, target, {
      duration: 1.5,
      ease: 'easeOut',
      onUpdate: (v) => setDisplayValue(Math.round(v)),
    });
    return controls.stop;
  }, [value, count]);

  const formatted = isCurrency
    ? `$${displayValue.toLocaleString()}`
    : displayValue.toLocaleString();

  return (
    <motion.span className="text-2xl font-bold text-white tabular-nums">
      {formatted}
    </motion.span>
  );
}
