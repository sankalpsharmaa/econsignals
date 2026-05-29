export interface Paper {
  id: number;
  title: string;
  authors: string[];
  abstract: string;
  doi: string | null;
  url: string | null;
  source: string | null;
  venue: string | null;
  published_at: string | null;
  score: number;
  zotero?: number | null;
  jel: string[];
  topics: string[];
  india: boolean;
}

export interface Deadline {
  name: string;
  type: 'funding' | 'cfp';
  organization: string | null;
  due: string | null;
  days_left: number | null;
  rolling: boolean;
  url: string | null;
  description: string;
  urgency: 'final' | 'urgent' | 'soon' | 'upcoming' | 'rolling';
}

export interface SocialItem {
  handle: string;
  source: 'mastodon' | 'twitter' | 'bluesky';
  content: string;
  url: string | null;
  paper_id: number | null;
  published_at: string | null;
}

export interface Sensor {
  sensor: string;
  status: string;
  last_run: string | null;
  items_found: number | null;
}

export interface FeedStats {
  papers: number;
  deadlines: number;
  social: number;
  personalized?: boolean;
}

export interface Feed {
  generated_at: string;
  stats: FeedStats;
  papers: Paper[];
  deadlines: Deadline[];
  social: SocialItem[];
  sensors: Sensor[];
}

export type Tab = 'papers' | 'deadlines' | 'social' | 'status';

export interface PaperFeedback {
  starred: boolean;
  vote: 'up' | 'down' | null;
  hidden: boolean;
}

export type FeedbackStore = Record<string, PaperFeedback>;

export type SortOrder = 'relevance' | 'newest' | 'title';
