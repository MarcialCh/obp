SELECT 
    author.name,
    save_circuit(provenance(), 'author_id', '{path}')
FROM 
    author, conference, publication, writes
WHERE
    conference.name = 'VLDB' 
    AND (publication.year < 1995 or publication.year > 2002 ) 
    AND publication.citation_num > 100
    AND author.aid = writes.aid 
    AND conference.cid = publication.cid 
    AND publication.pid = writes.pid
GROUP BY 
    author.name
LIMIT 10