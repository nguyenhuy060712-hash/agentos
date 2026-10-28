// Data model of the "Chuyên gia Agent OS" training programme (/dao-tao/).
// One source feeds the public module pages, the slide decks, the student handbook PDF and the instructor kit.
// Fields marked [GV] are instructor-only: they must never be rendered on public pages or in search.json;
// they appear only in the instructor build (INSTRUCTOR=1) and the instructor PDF.
export type Role = "user" | "admin" | "it" | "architect";
export type Track = "nen-tang" | "van-hanh" | "it" | "kien-truc";

export interface Section {
  id: string;            // anchor, kebab-case, unique within the module
  h: string;             // heading
  html: string;          // trusted HTML (p, ul, ol, table, strong, code, pre); tables are wrapped by the renderer
  diagram?: string;      // name of src/diagrams/<name>.svg
  diagramCaption?: string;
  shot?: string;         // screenshot key (src/assets/f or src/assets/shots)
  shotCaption?: string;
}

export interface QuizItem {
  q: string;
  options?: string[];    // multiple choice when present
  a: string;             // [GV] answer (for MCQ: the correct option text, plus why)
  rubric: string;        // [GV] how to grade (points, what earns full/partial credit)
}

export interface Slide {
  t: string;             // slide title
  pts?: string[];        // bullet points (short, plain text)
  diagram?: string;      // diagram name
  shot?: string;         // screenshot key
  code?: string;         // very short code sample
  note?: string;         // [GV] speaker note
}

export interface PlanStep { min: number; act: string; detail: string } // [GV] lesson plan, minutes sum to 180

export interface Module {
  n: number;             // session number 1..12
  slug: string;
  track: Track;
  roles: Role[];
  hours: number;
  title: string;
  short: string;         // ≤ 4 words, for navigation
  summary: string;
  goals: string[];       // measurable: "Học viên làm được …"
  prereq: string[];
  sections: Section[];
  lab: {
    title: string;
    time: string;
    setup?: string;      // what must exist before the lab
    steps: string[];     // trusted HTML per step
    result: string;      // expected result
    shot?: string;
    shotAlt?: string;
    check: string[];     // [GV] what the instructor checks to sign the lab off
  };
  pitfalls: [string, string][]; // [symptom, cause and fix]
  quiz: QuizItem[];
  homework: string[];
  glossary: [string, string][];
  slides: Slide[];
  plan: PlanStep[];      // [GV]
  ask: string[];         // [GV] open questions to start discussion
  notes: string[];       // [GV] teaching notes
}
