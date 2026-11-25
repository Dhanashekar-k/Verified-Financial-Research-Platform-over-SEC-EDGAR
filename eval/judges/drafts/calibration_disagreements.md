# Judge calibration: disagreements

n = 46, agreement = 41/46, Cohen's kappa = 0.646

## fb-01009: human `correct`, judge `refused`

- **Question:** What are the geographies that Pepsico primarily operates in as of FY2022?
- **Gold:** As of FY2022, Pepsico primarily operates in the following geographies: North America, Latin America, Europe, Africa, Middle East, South Asia, Asia Pacific, Australia, New Zealand and China.
- **Answer:** The FY2022 passages say only that PepsiCo serves customers and consumers in more than 200 countries and territories worldwide [3]. They do not name specific primary regions. The FY2022 subsidiary list (Exhibit 21) shows entities in jurisdictions including the United States, Mexico, Brazil, Argentina, Guatemala, Panama, the United Kingdom, Luxembourg, Hong Kong, Australia, Kyrgyzstan, Bermuda and the Cayman Islands [7]. For context, the FY2020 10-K organized operations into North America (US and Canada), Latin America, Europe, Africa/Middle East/South Asia, and Asia Pacific/Australia/New Zealand/China [6]. The passages do not confirm this segment structure for FY2022.
- **Refusal:** null
- **Judge reason:** The answer says the FY2022 passages do not name primary regions or confirm the segment structure for FY2022, so it declines to answer from the filings, even though it mentions FY2020 segment regions as context.

## fb-01107: human `correct`, judge `incorrect`

- **Question:** Has CVS Health reported any materially important ongoing legal battles from 2022, 2021 and 2020?
- **Gold:** Yes, CVS Health has been involved in multiple ongoing legal battles. Some notable legal dispute areas for CVS are: (1) usual and customary pricing litigation: where it's claimed that CVSâs retail pharmacies overcharged for prescription drugs; (2) PBM litigation and investigations: where it's claimed that that rebate agreements between the drug manufacturers and PBMs caused inflated prices for certain drug products; and (3) controlled substances litigation: legal matters around opioids for which CVS has agreed to pay up to $4.3 billion to claimants in remediation and $625 million to attorneys and fees
- **Answer:** **Yes for FY2022; the passages give no legal-proceedings detail for 2021 or 2020.**

- **Opioid litigation:** CVS is a defendant in the consolidated MDL (No. 2804) in the Northern District of Ohio and in similar state cases and state Attorney General suits [1].
  - In August 2022, a federal court in Ohio entered a $651 million judgment against three chain pharmacy defendants jointly, payable over 15 years. The Company is appealing and has not accrued a liability for it [1].
  - In March 2022, CVS settled all opioid claims by the State of Florida for $484 million, payable over 18 years [1].
- **Stockholder matters:** Since February 2019, securities class actions and a derivative complaint have alleged omissions and misrepresentations about the performance of the LTC business unit. The Company is defending them [6].
- **ERISA class actions:** Two ERISA class actions were filed in August and September 2020 in the District of Connecticut against CVS Health, Aetna and certain executives and directors [6].

The FY2021 passage provided is only an auditor's report, so it contains no legal-proceedings detail for 2021 or 2020 [8].
- **Refusal:** null
- **Judge reason:** The answer reports opioid litigation but gives different figures ($651M judgment, $484M Florida) than the gold's $4.3B/$625M settlement, and it omits the gold's key areas (usual and customary pricing litigation and PBM litigation), while saying 2021 and 2020 have no detail.

## fb-00215: human `incorrect`, judge `correct`

- **Question:** Is Verizon a capital intensive business based on FY 2022 data?
- **Gold:** Yes. Verizon's capital intensity ratio was approximately 2.774729. This means that it took approximately $2.77 of assets to generate $1 of revenue and thus, Verizon can be considered capital intensive.
- **Answer:** Yes, based on the passages, Verizon appears capital intensive in FY2022: it invested $23.1 billion in capital expenditures during 2022, including about $6.2 billion of C-Band related spending [7]. The passages also show large ongoing capital needs, with a 2022 capital program guided at $16.5–$17.5 billion plus an additional $5.0–$6.0 billion for C-Band [5]. Long-term debt commitments were $150.2 billion as of December 31, 2021 [5]. The passages do not give FY2022 revenue or total assets, so capex intensity as a ratio cannot be computed from them.
- **Refusal:** null
- **Judge reason:** The answer reaches the gold conclusion (Yes, Verizon is capital intensive in FY2022) and supports it with large capex figures, though it does not give the 2.77 capital intensity ratio and says it cannot be computed from the passages.

## fb-01328: human `correct`, judge `incorrect`

- **Question:** What is the quantity of restructuring costs directly outlined in Pepsico's income statements for FY2022? If restructuring costs are not explicitly outlined then state 0.
- **Gold:** Pepsico's restructuring costs in FY2022 amounted to $411 million .
- **Answer:** **0** – PepsiCo does not appear to show restructuring as its own income statement line for FY2022. The passages show the FY2022 restructuring and impairment charges of $411 million spread across existing line items: $33 million in cost of sales, $347 million in SG&A and $31 million in other pension and retiree medical benefits expense [1]. The passages do not include the income statement itself, so this rests on that breakdown.
- **Refusal:** null
- **Judge reason:** The gold answer is $411 million, but the system answer's final answer is 0, even though it mentions the $411 million charge being spread across other line items.

## fb-01254: human `correct`, judge `incorrect`

- **Question:** Has MGM Resorts paid dividends to common shareholders in FY2022?
- **Gold:** Yes. MGM maintained 0.01$ per share annual dividend through out FY 2022.
- **Answer:** Yes, in part of the year at least. The passages show MGM Resorts paid dividends to common shareholders in 2022. The statement of stockholders' equity, which starts from the April 1, 2022 balance, records dividends declared and paid of $0.0025 per share, totaling about $1.0 million ($1,021 thousand) [5]. The FY2022 10-K excerpt provided [1] only reaches the 2020 entries, so the passages do not give a full-year 2022 total.
- **Refusal:** null
- **Judge reason:** The gold says MGM paid $0.01 per share annually throughout FY2022, but the answer only confirms a $0.0025 quarterly dividend for part of the year and says the full-year total is not given, so it does not state the key fact of a consistent full-year dividend.

