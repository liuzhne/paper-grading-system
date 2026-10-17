<script setup>
/** 有据智评的标志：两个对角括角 + 一个对勾。 */
defineProps({
  size: { type: Number, default: 26 },
  strokeWidth: { type: Number, default: 2.1 },
});
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path d="M4 8.6V4h4.6" />
    <path d="M20 15.4V20h-4.6" />
    <path d="M8.2 12.4l3 3 5.2-5.8" />
  </svg>
</template>
