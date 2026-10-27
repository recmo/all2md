use pulldown_cmark::{Event, Options, Parser, Tag};
use regex::Regex;

use crate::{DateOrder, SectionListRule};

pub(crate) fn validate_list(
    text: &str,
    offset: usize,
    offsets: &[usize],
    rule: &SectionListRule,
    report: &mut impl FnMut(usize, String),
) {
    let marker = Regex::new(r"^(?:[-+*]|[0-9]{1,9}[.)])[ \t]+").expect("constant regex");
    let pattern = rule
        .item_pattern
        .as_ref()
        .map(|pattern| Regex::new(pattern).expect("validated regex"));
    let mut depth = 0_usize;
    let mut items = 0;
    let mut previous = None;
    let mut parser = Parser::new_ext(text, Options::all())
        .into_offset_iter()
        .peekable();
    while let Some((event, range)) = parser.next() {
        let line = offsets.partition_point(|start| *start <= offset + range.start);
        match event {
            Event::Start(tag) => {
                if depth > 0 && matches!(tag, Tag::Heading { .. }) {
                    report(line, "list section must not contain subsections".into());
                }
                if depth == 0 {
                    if let Tag::List(start) = &tag {
                        if rule
                            .ordered
                            .is_some_and(|ordered| ordered != start.is_some())
                        {
                            report(
                                line,
                                "list marker style does not match ordered setting".into(),
                            );
                        }
                    } else {
                        report(
                            line,
                            "body must contain only lists (no prose or subsections)".into(),
                        );
                    }
                }
                if depth == 1 && matches!(tag, Tag::Item) {
                    items += 1;
                    let raw = &text[range];
                    let content = marker.find(raw).map_or(raw, |marker| &raw[marker.end()..]);
                    if pattern
                        .as_ref()
                        .is_some_and(|pattern| !pattern.is_match(content))
                    {
                        report(line, "item does not match item_pattern".into());
                    }
                    if let Some(order) = rule.date_order {
                        let plain_start = matches!(
                            parser.peek(),
                            Some((Event::Text(_) | Event::Start(Tag::Paragraph), _))
                        );
                        if let Some(date) = date_prefix(content).filter(|_| plain_start) {
                            if previous.is_some_and(|previous| match order {
                                DateOrder::Ascending => date < previous,
                                DateOrder::Descending => date > previous,
                            }) {
                                report(line, "item date is out of order".into());
                            }
                            previous = Some(date);
                        } else {
                            report(line, "item must start with a valid YYYY-MM-DD date followed by whitespace or end of item".into());
                        }
                    }
                }
                depth += 1;
            }
            Event::End(_) => depth -= 1,
            _ if depth == 0 => report(line, "body must contain only lists".into()),
            _ => {}
        }
    }
    if items < rule.minimum_items {
        report(
            offsets.partition_point(|start| *start <= offset),
            format!("list has {items} items; minimum is {}", rule.minimum_items),
        );
    }
}

fn date_prefix(content: &str) -> Option<&str> {
    let bytes = content.as_bytes();
    if bytes.len() < 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !bytes[..10]
            .iter()
            .enumerate()
            .all(|(i, b)| i == 4 || i == 7 || b.is_ascii_digit())
        || content[10..]
            .chars()
            .next()
            .is_some_and(|c| !c.is_whitespace())
    {
        return None;
    }
    let year: u16 = content[..4].parse().ok()?;
    let month: usize = content[5..7].parse().ok()?;
    let day: u8 = content[8..10].parse().ok()?;
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    (year > 0 && (1..=12).contains(&month) && day > 0 && day <= days[month - 1])
        .then_some(&content[..10])
}
