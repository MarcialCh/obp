SELECT 
    conference.name,
    save_circuit(provenance(), 'conference_id', '{path}') 
FROM 
    author, organization, writes, publication, conference, publication_keyword, domain_conference, domain_keyword, keyword as keyword1, keyword as keyword2
WHERE
    author.oid = organization.oid
    AND author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.cid = conference.cid
    AND publication.pid = publication_keyword.pid
    AND publication_keyword.kid = keyword1.kid
    AND conference.cid = domain_conference.cid
    AND domain_conference.did = domain_keyword.did
    AND domain_keyword.kid = keyword2.kid
    AND keyword1.keyword = keyword2.keyword
    AND organization.name = 'University of Michigan'
    AND publication.year > 2010
    AND publication.citation_num > 3
GROUP BY conference.name 
LIMIT 10