export const fps = 30;
export const chapters = [
  {id:'mission', name:'Your task', seconds:14, title:'Run a customer conversation', labels:['Your team','Judge as customer'],
    beats:['Your judge plays the customer on your scenario card. Ask about their business and listen to what they tell you.',
      'In the preliminary round you have 10 minutes to pitch, then 3 minutes for feedback. Aim to agree a Discovery meeting.'],
    task:'Open your own scenario pack and read the customer card together.', target:'resources', link:'Open Resources'},
  {id:'team', name:'Plan together', seconds:16, title:'Agree who says what', labels:['Read the brief','Write four cues','Rehearse aloud'],
    beats:['Read the brief together. Agree who leads each step and how you will hand over. Everyone must speak.',
      'Use a few short cues per step. Leave time for one rehearsal aloud. Ask a coach if you get stuck.'],
    task:'Name the speaker or speakers for each step in My Pitch.', target:'worksheet', link:'Open team worksheet'},
  {id:'discover', name:'Discover', seconds:16, title:'Find out why now', step:0, labels:['Who uses laptops?','What changed?','Who decides?'],
    beats:['Pick the questions you need. Ask who uses laptops, what changed, and what has to work. Find out who decides and what IT can support.',
      'Listen to their answers and repeat back the priority. Use it to decide whether they have small or outsourced IT (Type A) or dedicated IT (Type B).'],
    task:'Save your opening questions and one follow-up in Discover.', target:'discover', link:'Open Discover in My Pitch'},
  {id:'mac', name:'Solution at Work', seconds:16, title:'Make solution device relevant', step:1, labels:['People','IT','Business'],
    beats:['Connect one benefit each to the customer’s people, IT and business. Use the need they just told you about.',
      'Recommend a suitable solution device for two roles. Explain the fit in their words. For field staff, that could mean getting through a site visit on battery.'],
    task:'Save your three benefits and two role recommendations.', target:'mac', link:'Open Solution at Work in My Pitch'},
  {id:'solve', name:'Solve', seconds:16, title:'Handle the concern', step:2, labels:['Acknowledge','Check','Offer a next step'],
    beats:['Prepare for one IT concern and one business concern. Listen, acknowledge it, and check what the customer needs to see.',
      'Name a relevant implementation partner service. For an app concern, offer to check their critical apps in Discovery before discussing a rollout.'],
    task:'Practise the example below, then save your two likely objections and responses.', target:'solve', link:'Open Solve in My Pitch'},
  {id:'close', name:'Close', seconds:16, title:'Ask for a date', step:3, labels:['Why meet?','Who joins?','When?'],
    beats:['Ask for a free, no-obligation 45-minute Discovery meeting. Explain how it helps with the customer’s specific priority.',
      'Invite the customer’s IT team and implementation partner Technical Services. Ask who will attend and propose a day.'],
    task:'Write the exact meeting request you want your team to practise.', target:'close', link:'Open Close in My Pitch'},
  {id:'workspace', name:'Use the workspace', seconds:18, title:'Get your cues ready',
    beats:['Open Resources for your scenario pack. In My Pitch, choose a step, enter the speaker and cues, then select Save for the team.',
      'Today shows your station and round. When called, open Pitch Mode. It shows your cues and follows your judge’s clock.'],
    task:'Rehearse all four handoffs and check that your teammate can see the saved cues.', target:'worksheet', link:'Open team worksheet'}
];
export const starts = chapters.map((_, i) => chapters.slice(0,i).reduce((frames, chapter) => frames + chapter.seconds * fps, 0));
export const duration = starts.at(-1) + chapters.at(-1).seconds * fps;
