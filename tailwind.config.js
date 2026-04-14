tailwind.config = {
    theme: {
        extend: {
            fontFamily: {
                serif: ['Fraunces', 'Georgia', 'serif'],
                sans: ['Lora', 'Georgia', 'serif'],
            },
            colors: {
                canvas: 'oklch(var(--canvas-oklch) / <alpha-value>)',
                ink: 'oklch(var(--ink-oklch) / <alpha-value>)',
                'ink-light': 'oklch(var(--ink-light-oklch) / <alpha-value>)',
                'ink-lighter': 'oklch(var(--ink-lighter-oklch) / <alpha-value>)',
                'warm-gray': 'oklch(var(--warm-gray-oklch) / <alpha-value>)',
                'warm-gray-light': 'oklch(var(--warm-gray-light-oklch) / <alpha-value>)',
                accent: 'oklch(var(--accent-oklch) / <alpha-value>)',
                'accent-light': 'oklch(var(--accent-light-oklch) / <alpha-value>)',
            },
        },
    },
};
