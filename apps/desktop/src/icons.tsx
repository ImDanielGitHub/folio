import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function IconBase({ size = 18, children, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const SourceIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M5 3.8h10.5L19 7.3v12.9H5z" />
    <path d="M15.5 3.8v3.7H19M8 12h8M8 15.5h6" />
  </IconBase>
);

export const ActivityIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M4 12a8 8 0 1 0 2.3-5.7L4.4 8.2" />
    <path d="M4 4.8v3.4h3.4M12 7.5V12l3 2" />
  </IconBase>
);

export const PrivacyIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M12 3.4 19 6v5.2c0 4.2-2.6 7.6-7 9.4-4.4-1.8-7-5.2-7-9.4V6z" />
    <path d="m9.2 12 1.7 1.7 3.9-4" />
  </IconBase>
);

export const BriefIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M4.5 4.5h15v15h-15zM8 9h8M8 12h5M8 15h7" />
  </IconBase>
);

export const CashIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M4 18.5V6.4M4 18.5h16" />
    <path d="m6.5 15 4-4 3 2.2 5-6" />
  </IconBase>
);

export const RecordsIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M4 5h16v14H4zM4 9.5h16M9 5v14M14 5v14" />
  </IconBase>
);

export const PackIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M7 3.8h8l3 3V20H7zM15 3.8v3h3M10 11h5M10 14h5M10 17h3" />
  </IconBase>
);

export const CloseIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="m7 7 10 10M17 7 7 17" />
  </IconBase>
);

export const SendIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="m4 5 16 7-16 7 3-7zM7 12h13" />
  </IconBase>
);

export const StopIcon = (props: IconProps) => (
  <IconBase {...props}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="1" />
  </IconBase>
);

export const ArrowIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="m9 5 7 7-7 7" />
  </IconBase>
);

export const CheckIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="m5 12.5 4.2 4L19 7" />
  </IconBase>
);

export const SparkIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M12 3.5c.5 4.3 2.2 6 6.5 6.5-4.3.5-6 2.2-6.5 6.5-.5-4.3-2.2-6-6.5-6.5 4.3-.5 6-2.2 6.5-6.5Z" />
    <path d="M18 15.5c.2 1.7.8 2.3 2.5 2.5-1.7.2-2.3.8-2.5 2.5-.2-1.7-.8-2.3-2.5-2.5 1.7-.2 2.3-.8 2.5-2.5Z" />
  </IconBase>
);

export const LinkIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M9.5 14.5 14.5 9M8.2 16.8l-1.1 1.1a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0M15.8 7.2l1.1-1.1a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" />
  </IconBase>
);

export const MoreIcon = (props: IconProps) => (
  <IconBase {...props}>
    <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
  </IconBase>
);

export const DownloadIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M12 4v11M8 11l4 4 4-4M5 19h14" />
  </IconBase>
);

export const UndoIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M8.5 8H4V3.5M4.5 7.5A8 8 0 1 1 4 13" />
  </IconBase>
);

export const TelegramIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="m3 11.5 17-7-3.5 15-5-4-3 2.5.5-4.5zM9 13.5 16 8" />
  </IconBase>
);

export const WarningIcon = (props: IconProps) => (
  <IconBase {...props}>
    <path d="M12 4 21 20H3zM12 9v5M12 17.2v.2" />
  </IconBase>
);
